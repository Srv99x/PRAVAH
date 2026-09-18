"""
scripts/capture_screenshots.py
───────────────────────────────
Capture three slide-ready screenshots of the PRAVAH Streamlit app:

    docs/slides-assets/ss_map.png      map + title + legend
    docs/slides-assets/ss_explain.png  "Why is this cell flagged?" panel for KM_R020_C018
    docs/slides-assets/ss_ranked.png   "Ranked review queue" panel (metrics + top-10 table)

This script owns the whole lifecycle: it starts app/streamlit_app.py itself
on port 8599, waits for it to respond, drives it with Playwright, and shuts
it down again at the end (even on failure).

Run from the repository root:

    python scripts/capture_screenshots.py

Requires (see requirements.txt, "# dev" section):
    pip install playwright
    playwright install chromium
"""

from __future__ import annotations

import re
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import requests
from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs" / "slides-assets"

PORT = 8599
BASE_URL = f"http://localhost:{PORT}"
STARTUP_TIMEOUT_S = 90

VIEWPORT = {"width": 1920, "height": 1080}
DEVICE_SCALE_FACTOR = 2

EXPLAIN_GRID_ID = "KM_R020_C018"
MAP_TITLE_TEXT = (
    "🗺️ Storm-triggered priority map (flash flood & landslide) — Kamrup Metro"
)
EXPLAIN_HEADING_TEXT = "🔎 Why is this cell flagged?"
RANKED_HEADING_TEXT = "⚠️ Ranked review queue"

CLIP_PADDING = 6


# ══════════════════════════════════════════════════════════════════════════
# STREAMLIT SUBPROCESS
# ══════════════════════════════════════════════════════════════════════════

def wait_for_server(url: str, timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            resp = requests.get(url, timeout=2)
            if resp.status_code == 200:
                return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(0.5)
    return False


def start_streamlit() -> subprocess.Popen:
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "streamlit", "run", "app/streamlit_app.py",
            "--server.port", str(PORT),
            "--server.headless", "true",
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    print(f"Starting Streamlit on {BASE_URL} (pid {proc.pid}) ...")
    if not wait_for_server(BASE_URL, STARTUP_TIMEOUT_S):
        proc.terminate()
        out = proc.stdout.read() if proc.stdout else ""
        raise RuntimeError(
            f"Streamlit never responded on {BASE_URL} within "
            f"{STARTUP_TIMEOUT_S}s.\n--- subprocess output ---\n{out}"
        )
    print("Streamlit is up.")
    return proc


def stop_streamlit(proc: subprocess.Popen) -> None:
    print("Stopping Streamlit ...")
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    print("Streamlit stopped.")


# ══════════════════════════════════════════════════════════════════════════
# DOM MEASUREMENT HELPERS
# ══════════════════════════════════════════════════════════════════════════
# Streamlit wraps every top-level st.* call in a div[data-testid=
# "stElementContainer"]. We locate the leaf text node for a given string and
# measure its enclosing container's box in CURRENT-viewport (scroll-relative)
# coordinates via getBoundingClientRect() — so every box getter below must be
# re-called after any scroll, never cached across a scroll.

_LEAF_EXACT_JS = """(text) => {
    const all = [...document.querySelectorAll('body *')];
    const el = all.find(e => e.children.length === 0 && e.textContent
                              && e.textContent.trim() === text);
    if (!el) return null;
    const cont = el.closest('[data-testid="stElementContainer"]') || el;
    const r = cont.getBoundingClientRect();
    return {top: r.top, left: r.left, right: r.right, bottom: r.bottom};
}"""

_ROW_EXACT_JS = """(text) => {
    const all = [...document.querySelectorAll('body *')];
    const el = all.find(e => e.children.length === 0 && e.textContent
                              && e.textContent.trim() === text);
    if (!el) return null;
    const row = el.closest('[data-testid="stHorizontalBlock"]')
                || el.closest('[data-testid="stElementContainer"]') || el;
    const r = row.getBoundingClientRect();
    return {top: r.top, left: r.left, right: r.right, bottom: r.bottom};
}"""

_INNERMOST_CONTAINING_JS = """(text) => {
    const all = [...document.querySelectorAll('div,span,p')];
    const matches = all.filter(e => e.textContent && e.textContent.includes(text));
    if (!matches.length) return null;
    // querySelectorAll is document order (outer before inner) -> the LAST
    // match is the deepest / tightest-fitting element containing the text.
    const el = matches[matches.length - 1];
    const r = el.getBoundingClientRect();
    return {top: r.top, left: r.left, right: r.right, bottom: r.bottom};
}"""

_MAIN_BOUNDS_JS = """() => {
    const m = document.querySelector('[data-testid="stMainBlockContainer"]');
    const r = m.getBoundingClientRect();
    return {left: r.left, right: r.right};
}"""

_IFRAME_BOX_JS = """(title) => {
    const f = document.querySelector(`iframe[title="${title}"]`);
    if (!f) return null;
    const cont = f.closest('[data-testid="stElementContainer"]') || f;
    const r = cont.getBoundingClientRect();
    return {top: r.top, left: r.left, right: r.right, bottom: r.bottom};
}"""

_DATAFRAME_BOX_JS = """() => {
    const df = document.querySelector('[data-testid="stDataFrame"]');
    if (!df) return null;
    const cont = df.closest('[data-testid="stElementContainer"]') || df;
    const r = cont.getBoundingClientRect();
    return {top: r.top, left: r.left, right: r.right, bottom: r.bottom};
}"""


def _require(box: dict | None, what: str) -> dict:
    if box is None:
        raise RuntimeError(f"Could not locate on page: {what}")
    return box


def leaf_exact_box(page: Page, text: str) -> dict:
    return _require(page.evaluate(_LEAF_EXACT_JS, text), text)


def row_exact_box(page: Page, text: str) -> dict:
    return _require(page.evaluate(_ROW_EXACT_JS, text), text)


def innermost_containing_box(page: Page, text: str) -> dict:
    return _require(page.evaluate(_INNERMOST_CONTAINING_JS, text), text)


def main_bounds(page: Page) -> dict:
    return page.evaluate(_MAIN_BOUNDS_JS)


def iframe_box(page: Page, title: str) -> dict:
    return _require(page.evaluate(_IFRAME_BOX_JS, title), f"iframe[title={title!r}]")


def dataframe_box(page: Page) -> dict:
    return _require(page.evaluate(_DATAFRAME_BOX_JS), "stDataFrame")


def capture_section(
    page: Page,
    out_path: Path,
    top_box_fn: Callable[[], dict],
    bottom_box_fn: Callable[[], dict],
    pad: int = CLIP_PADDING,
) -> None:
    """
    Screenshot the region spanning from top_box_fn()'s top to bottom_box_fn()'s
    bottom, full main-content width. Boxes are measured fresh, scrolled into
    view, then re-measured (getBoundingClientRect is scroll-relative) so the
    final clip always matches what is actually on screen.
    """
    top_box = top_box_fn()
    # Streamlit's scroll container is the <section data-testid="stMain">,
    # not window/body (which never scroll here) — scroll that element.
    page.evaluate(
        """(y) => {
            const main = document.querySelector('[data-testid="stMain"]');
            main.scrollTop = Math.max(main.scrollTop + y - 40, 0);
        }""",
        top_box["top"],
    )
    page.wait_for_timeout(300)

    # Re-measure after the scroll.
    top_box = top_box_fn()
    bottom_box = bottom_box_fn()
    bounds = main_bounds(page)

    top = min(top_box["top"], bottom_box["top"]) - pad
    bottom = max(top_box["bottom"], bottom_box["bottom"]) + pad
    left = bounds["left"] - pad
    right = bounds["right"] + pad

    if bottom - top > VIEWPORT["height"]:
        # Whole section taller than one screen — scroll further isn't
        # possible with a single non-full-page clip, so fail loudly rather
        # than silently cut a row in half.
        raise RuntimeError(
            f"{out_path.name}: section height {bottom - top:.0f}px exceeds "
            f"viewport height {VIEWPORT['height']}px — cannot capture in one "
            "clip without cutting content."
        )

    clip = {
        "x": max(left, 0),
        "y": max(top, 0),
        "width": min(right, VIEWPORT["width"]) - max(left, 0),
        "height": bottom - max(top, 0),
    }
    page.screenshot(path=str(out_path), clip=clip)


# ══════════════════════════════════════════════════════════════════════════
# PNG SIZE (no extra dependency — read the IHDR chunk directly)
# ══════════════════════════════════════════════════════════════════════════

def png_pixel_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as fh:
        header = fh.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} is not a PNG file")
    width, height = struct.unpack(">II", header[16:24])
    return width, height


# ══════════════════════════════════════════════════════════════════════════
# PAGE INTERACTIONS
# ══════════════════════════════════════════════════════════════════════════

def set_replay_date_to_bonda(page: Page) -> None:
    button = page.get_by_role("button", name=re.compile("Bonda landslide"))
    button.click()
    # The click triggers st.rerun(); wait for the underlying native date
    # input (data-testid="hidden-dateinput-container") to show the new ISO
    # value before doing anything else.
    page.wait_for_function(
        """() => {
            const el = document.querySelector(
                '[data-testid="hidden-dateinput-container"] input');
            return el && el.value === '2025-05-30';
        }""",
        timeout=15000,
    )


def hide_app_chrome(page: Page) -> None:
    """
    Hide Streamlit's own header/toolbar (Deploy button, main menu, the
    sidebar-collapse chevron's bar) so it can never bleed into a clip
    screenshot regardless of scroll position — it is `position: fixed` to
    the viewport, not to the scrolling content.

    Also force every element container to full opacity. Streamlit fades an
    element-container to ~0.33 while it considers a downstream widget
    "stale" after a rerun (observed on the st_folium component container in
    particular); in headless automation this fade can still be mid-transition
    at capture time even after the content itself is fully correct, which
    would otherwise wash out the screenshot for no functional reason.
    """
    page.add_style_tag(
        content="""
        [data-testid="stHeader"] { display: none !important; }
        [data-testid="stElementContainer"],
        [data-testid="stElementContainer"] * {
            opacity: 1 !important;
        }
        """
    )


def collapse_sidebar(page: Page) -> None:
    page.locator('[data-testid="stSidebarCollapseButton"] button').click()
    page.wait_for_function(
        """() => {
            const sb = document.querySelector('[data-testid="stSidebar"]');
            return sb && sb.getAttribute('aria-expanded') === 'false';
        }""",
        timeout=5000,
    )
    page.wait_for_timeout(400)  # CSS collapse transition


def wait_for_map_ready(page: Page) -> None:
    frame = page.frame_locator('iframe[title="streamlit_folium.st_folium"]')
    frame.locator(".leaflet-container").wait_for(state="visible", timeout=20000)
    page.wait_for_timeout(3000)  # fixed settle, as specified


def select_grid_cell(page: Page, grid_id: str) -> None:
    combobox = page.locator('[data-testid="stSelectbox"] input')
    combobox.click()
    page.keyboard.press("Control+A")
    combobox.type(grid_id, delay=20)
    option = page.get_by_role("option", name=grid_id, exact=True)
    option.wait_for(state="visible", timeout=5000)
    option.click()
    # The selected value lives in the <input>'s value attribute, which is
    # NOT part of document.body.innerText — check the input directly.
    page.wait_for_function(
        """(gid) => {
            const el = document.querySelector('[data-testid="stSelectbox"] input');
            return el && el.value === gid;
        }""",
        arg=grid_id,
        timeout=15000,
    )
    # Give the resulting rerun time to repaint the explanation panel.
    page.wait_for_timeout(1500)


# ══════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════

def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    streamlit_proc = start_streamlit()

    saved: list[Path] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(
                viewport=VIEWPORT,
                device_scale_factor=DEVICE_SCALE_FACTOR,
            )
            # Streamlit keeps a persistent WebSocket open, so "networkidle"
            # never fires — wait for DOM content, then the sidebar itself.
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
            hide_app_chrome(page)
            page.wait_for_selector(
                '[data-testid="stSidebar"]', state="visible", timeout=30000
            )
            page.wait_for_timeout(1000)  # let the initial data load settle

            print("Setting replay date to 30 May 2025 (Bonda landslide) ...")
            set_replay_date_to_bonda(page)

            print("Collapsing sidebar ...")
            collapse_sidebar(page)

            print("Waiting for the map to finish rendering ...")
            wait_for_map_ready(page)

            # ── ss_map.png ──────────────────────────────────────────────
            print("Capturing ss_map.png ...")
            map_path = OUT_DIR / "ss_map.png"
            capture_section(
                page,
                map_path,
                top_box_fn=lambda: row_exact_box(page, MAP_TITLE_TEXT),
                bottom_box_fn=lambda: iframe_box(
                    page, "streamlit_folium.st_folium"
                ),
            )
            saved.append(map_path)

            # ── ss_explain.png ──────────────────────────────────────────
            print(f"Selecting grid cell {EXPLAIN_GRID_ID} ...")
            select_grid_cell(page, EXPLAIN_GRID_ID)

            print("Capturing ss_explain.png ...")
            explain_path = OUT_DIR / "ss_explain.png"
            capture_section(
                page,
                explain_path,
                top_box_fn=lambda: leaf_exact_box(page, EXPLAIN_HEADING_TEXT),
                bottom_box_fn=lambda: innermost_containing_box(
                    page, "White outline: this cell"
                ),
            )
            saved.append(explain_path)

            # ── ss_ranked.png ───────────────────────────────────────────
            print("Capturing ss_ranked.png ...")
            ranked_path = OUT_DIR / "ss_ranked.png"
            capture_section(
                page,
                ranked_path,
                top_box_fn=lambda: leaf_exact_box(page, RANKED_HEADING_TEXT),
                bottom_box_fn=lambda: dataframe_box(page),
            )
            saved.append(ranked_path)

            browser.close()
    finally:
        stop_streamlit(streamlit_proc)

    print("\nSaved:")
    for path in saved:
        width, height = png_pixel_size(path)
        print(f"  {path.relative_to(ROOT)}  ({width}x{height}px)")


if __name__ == "__main__":
    main()
