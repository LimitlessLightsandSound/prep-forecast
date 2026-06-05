# Limitless — Prep Workload Forecast

Self-updating shop-labor dashboard. Pulls active opportunities from Current RMS,
spreads each job's prep and de-prep labor across its scheduled days, and shows
hours + shop-hands needed per day. Refreshes every 3 hours via a Claude Code
routine. Dashboard hosted free on GitHub Pages.

## Pieces
| File | Role |
|------|------|
| `build_forecast.py` | The whole pipeline: RMS pull -> spread calc -> writes `docs/forecast.json` |
| `docs/index.html` | The dashboard (dark theme). Reads `forecast.json` next to it |
| `docs/forecast.json` | Output data, overwritten each run (sample committed so Pages renders day one) |
| `.claude/routine.md` | The every-3-hours Claude Code routine spec |

## Setup
1. Push this repo to GitHub.
2. **Settings -> Pages**: Source = Deploy from branch, Branch = `main`, Folder = `/docs`.
   Your dashboard URL: `https://<you>.github.io/<repo>/`
3. Set env vars where the routine runs:
   - `CURRENT_RMS_TOKEN` — System Setup > Integrations > API
   - `CURRENT_RMS_SUBDOMAIN` — your Current RMS subdomain
4. Confirm field names once: `DEBUG_FIELDS=1 python3 build_forecast.py`
5. Add the routine in `.claude/routine.md` to Claude Code, schedule every 3 hours.

## The model
- Per-item prep/de-prep minutes live in Current RMS custom fields
  `prep_labor_mins` / `deprep_labor_mins`, multiplied by line quantity.
- Totals spread evenly across each opp's scheduled prep days and return days.
- Shop hands = ceil(total daily minutes / shift). Shift defaults to 8h (`SHIFT_HOURS`).
- Window defaults to 14 days (`FORECAST_DAYS`).

No Zapier, no Google Sheet, no row-editing. Full recompute every run = always current.
