# PRAVAH retrospective forecast backtest

This is a **latency-assumed retrospective evaluation** using pinned `ecmwf_ifs` data. Open-Meteo describes part of the Single Runs archive as IFS Cycle 49R1 hindcasts, so the result does not prove that these forecasts or this pipeline were available on the historical issue dates. The 2025 period was previously evaluated with ERA5-domain inputs and is therefore a reused test period rather than a pristine confirmatory test.

The primary latency assumption is 7 hours after model initialization. The threshold was selected only from 2024 forecast-domain data and frozen before 2025 predictions were assembled. The shipped `models/random_forest_trigger_model.pkl` was not read.

## 2025 result at the primary latency

| Lead | Coverage | Episode recall (Wilson 95% interval) | Baseline recall | Maximum monthly false-alert days | Decision |
|---:|---:|---:|---:|---:|---|
| 6 h | 97.0% | 0 of 5 (0.0%; Wilson 95% CI 0.0%–43.4%) | 0 of 5 (0.0%) | 0 | lead time not established |
| 12 h | 97.0% | 0 of 5 (0.0%; Wilson 95% CI 0.0%–43.4%) | 0 of 5 (0.0%) | 0 | lead time not established |
| 24 h | 97.0% | 0 of 5 (0.0%; Wilson 95% CI 0.0%–43.4%) | 0 of 5 (0.0%) | 0 | lead time not established |

Each scheduled alert remains valid until the next 12-hourly target. Episodes are paired with the latest target at or before onset, so the named lead is a minimum; actual issue-to-onset lead is retained per episode in the JSON. API or feature gaps count as misses. Coverage-conditional recall and all denominators are also reported.

False-alert days are unique Asia/Kolkata target dates with at least one district alert that does not intersect an onset event at that scheduled target hour. They are false alerts against the rainfall-threshold proxy, not verified false landslide warnings.

Recall@10 is weak supporting evidence. Four of the seven dated incidents predate the evaluation archive, and the five model features are shared by many cells, producing large ranking ties. The JSON reports the frozen `grid_id` tie-break and optimistic boundary-tie sensitivity separately.

The 5-hour and 9-hour latency results are sensitivity analyses only. No parameter was changed after 2025 results were calculated.

<!-- forecast-diagnostics:start -->
## Why recall was zero

**Exploratory diagnostic only; this does not revise the frozen gate.** The 2024 calibration selected the endpoint threshold 1.0. The separate diagnostic compares forecast and ERA5 rainfall at every episode onset and sweeps lower thresholds against the alert budget.

Across all 17 reference episodes, forecast rainfall crossed the 20 mm/1 h or 60 mm/3 h threshold at any tested lead for **0 of 17 episodes**. The maximum forecast-to-observed extreme ratios were **0.370** for 1-hour rainfall and **0.337** for 3-hour rainfall. See [`forecast_diagnostics.md`](forecast_diagnostics.md) for the episode-level input comparison, 2024 calibration sweep, clearly post-test 2025 sensitivity, corrected Wilson intervals, and coverage details.

The 2024 sweep shows this was not simply a calibration failure caused by selecting the endpoint: no tested threshold recovered an episode at any lead while satisfying the four-false-alert-days-per-month budget. At a much looser 15-day budget, the best calibration recalls were only 1 of 12, 2 of 12, and 1 of 12 episodes at 6, 12, and 24 hours. This indicates no useful forecast-domain skill under the stated alert budget.

The raw 2025 result remains **0 of 5 episodes** at every lead. Its Wilson 95% interval is **0.0%–43.4%**, rather than the degenerate percentile-bootstrap interval. The lower bound and gate verdict are unchanged: **lead time not established**.
<!-- forecast-diagnostics:end -->
