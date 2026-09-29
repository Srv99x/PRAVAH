import asyncio
from playwright.async_api import async_playwright

URL = "https://pravah-luit.streamlit.app/"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(URL, timeout=120_000)
        await page.wait_for_timeout(8_000)
        wake = page.get_by_role("button", name="Yes, get this app back up!")
        if await wake.count():
            print("App was asleep - waking it")
            await wake.click()
            await page.wait_for_timeout(90_000)
        else:
            print("App is awake")
            await page.wait_for_timeout(20_000)
        await browser.close()


asyncio.run(main())
