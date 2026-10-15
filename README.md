# Vanilai

A Home Assistant weather integration that fuses many forecasts into one, tells you how far to trust it, and learns which sources are right at your location.

## What it does
- **Sources:** Open-Meteo (8+ models), MET Norway, US National Weather Service, and any `weather.*` entities you already have (hourly, falling back to daily).
- **Fusion:** one vote per independent model family; exact copies of another source are dropped.
- **Confidence:** from temperature spread *and* disagreement about rain.
- **Learning:** forecasts are scored against real observations (your own temperature sensor, else the nearest METAR station). Accurate sources gain weight; new ones start on probation; poor ones are demoted.
- **Resilience:** hourly polling, retries with backoff, last good forecast served (flagged stale) if every source fails, and a quick re-check when other integrations are still starting.
- **Location:** HA home, or follow a person/device tracker; coordinates are rounded to ~1 km before leaving HA.

## Install
Copy `custom_components/vanilai` into your HA config (or add the repo to HACS), restart, then Settings → Devices & services → Add integration → Vanilai.

## Extras
- `blueprints/automation/vanilai/rain_alert.yaml`: notify shortly before rain.
- `dashboard/comparison.yaml`: Lovelace stack (needs `apexcharts-card`).

## Testing
- `tests/test_logic.py`: unit tests for the fusion/verification logic.
- `tests/e2e/run_e2e.py`: starts a throwaway HA container, runs the real config flow, and checks the entities (needs Docker; `--keep` leaves the container up, `--option key=json` sets integration options).
