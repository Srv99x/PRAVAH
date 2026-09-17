PROJECT RULES (PRAVAH, SIH26192)
- Never invent metrics, data, coordinates, or government integrations.
- Every number shown in the app, README, or slides must come from a committed script output (docs/*.json or docs/*.md).
- The score is an ordinal "priority index", never a "probability" or "risk %".
- Historical replay is not a forecast. Never label ERA5 dates as "Forecast".
- Missing or stale data must never display as low priority.
- Telemetry is SIMULATED unless a real device is connected; label it.
- Do not use models/random_forest_trigger_model.pkl for evaluation (it was trained on a different split).
- Do not touch files outside the task's scope. Run tests before finishing.