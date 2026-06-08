# Limitless — Prep Workload Forecast

Self-updating shop-labor dashboard. Pulls active opportunities from Current RMS,
spreads each job's prep and de-prep labor across its scheduled days, and shows
hours + shop-hands needed per day. Refreshes every 3 hours via a Claude Code
routine. Dashboard hosted free on GitHub Pages.

## Pieces
| File | Role |
|------|------|
| `build_forecast.py` | RMS pull -> spread calc -> writes `docs/forecast.json` |
| `build_deliveries.py` | Lasso pull -> upcoming truck drives -> writes `docs/deliveries.json` |
| `explore_lasso.py` | Read-only probe for poking at the Lasso API (dev tool) |
| `docs/index.html` | Prep forecast dashboard (dark theme). Reads `forecast.json` |
| `docs/deliveries.html` | Truck deliveries dashboard. Reads `deliveries.json` |
| `docs/forecast.json` / `docs/deliveries.json` | Output data, overwritten each run (samples committed so Pages renders day one) |
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

## Working locally
The cloud routine pushes a new encrypted `docs/forecast.json` to `master` every 3
hours, so **always pull before you start editing** — otherwise your push collides
with the routine's.

1. Open the repo folder in VS Code (`File -> Open Folder -> prep-forecast`), or
   from a terminal: `code ~/Documents/prep-forecast`.
2. `git pull` — get the latest, including the routine's most recent forecast.
3. Make changes, then `git add`, `git commit`, `git push`.
   - Pushing to `master` triggers GitHub Pages to redeploy the dashboard.
   - The data refresh runs in the cloud independently — nothing to start locally.

If a pull/push ever conflicts, it'll be on `docs/forecast.json`. That file is
regenerated in full every run, so just take the remote version:
`git checkout --theirs docs/forecast.json` (or discard your local copy of it).
