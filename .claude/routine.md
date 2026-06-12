# Prep Forecast — scheduled routine

Run **every 3 hours**. Rebuilds the prep/de-prep workload forecast from Current
RMS and publishes it to the dashboard. No manual steps.

## Schedule
Every 3 hours (00:00, 03:00, 06:00, 09:00, 12:00, 15:00, 18:00, 21:00 local).

## Runs as a Claude cloud routine (no local machine)
This runs on Anthropic-managed cloud infrastructure every 3 hours — laptop can be
off. Credentials are provided as the routine's **cloud environment variables**
(not from a local `.env`, which is gitignored and not in the repo).

### Routine cloud environment (set in claude.ai > routine > environment)
```
CURRENT_RMS_TOKEN=<your RMS API token>
CURRENT_RMS_SUBDOMAIN=limitless
FORECAST_PASSPHRASE=<the shared passphrase that unlocks the dashboard>
LASSO_API_TOKEN=<your Lasso API key — for the Truck Deliveries page>
```
Repo access: grant the routine `LimitlessLightsandSound/prep-forecast` and enable
**"Allow unrestricted branch pushes"** so it can push to `master`.

### Cloud-environment settings that are easy to get wrong (verified working)
- **Network access = Custom**, with BOTH `api.current-rms.com` AND
  `limitless.lasso.io` in the allowlist, AND keep "include default package
  managers" checked. Trusted/None blocks the calls with `403 Host not in
  allowlist`. (limitless.lasso.io is the Lasso Workforce API for the deliveries
  page; api.current-rms.com is the RMS pull for the forecast.)
- **Setup script = `pip install cryptography`** — NOT `pip install -r
  requirements.txt`. The setup script runs before the repo is checked out, so the
  requirements file isn't present yet; installing the package by name avoids the
  `No such file or directory: 'requirements.txt'` failure.
- GitHub connection is separate from the App install: the claude.ai account must
  be linked to the GitHub identity (claude.ai/settings/connected-accounts) before
  the repo appears in the routine's repository picker.

### Routine prompt
1. In the `prep-forecast` repo on branch `master`, install deps and run BOTH builds:
   `pip install -r requirements.txt && python3 build_forecast.py && python3 build_deliveries.py`
   (`CURRENT_RMS_TOKEN`, `CURRENT_RMS_SUBDOMAIN`, `FORECAST_PASSPHRASE`,
   `LASSO_API_TOKEN` are already in the environment.) With the passphrase set,
   both `docs/forecast.json` (from Current RMS) and `docs/deliveries.json` (from
   the Lasso Workforce API) are written ENCRYPTED (AES-256-GCM) — safe to publish.
2. Commit `docs/forecast.json` and `docs/deliveries.json` with message
   `forecast: <current UTC timestamp>` and push to `master`. (The ciphertext
   changes every run by design — a random salt/IV each time — so expect a commit
   on every run.)
3. Do not modify any other file. Both scripts are read-only against their APIs
   (GET only, host-pinned); they never write to Current RMS or Lasso.

GitHub Pages (PUBLIC repo, branch `master`, `/docs`) serves the dashboard at
https://limitlesslightsandsound.github.io/prep-forecast/ and rebuilds on each
push. The published data is encrypted; the page prompts for the shared passphrase
and decrypts in-browser. Free plan is fine — no private Pages needed because the
data is never published in the clear.

## Vendor-history cache (suggested vendors on shortages)
`docs/vendor_history.json` maps each item to the suppliers we've sub-rented it from
over the last 18 months (ranked). The Shortages tab shows these as "↻ sourced before"
chips. It is built by a SEPARATE script — `build_vendor_history.py` — because the
crawl is slow (a few hundred nested API calls, several minutes). The 3-hour pipeline
only READS the cached file and joins it to the current shortages, so it stays fast.

- The 3-hour routine does NOT rebuild this cache; it reads whatever is committed.
- Refresh occasionally (vendor relationships move slowly — weekly/monthly is plenty):
  `source .env && python3 build_vendor_history.py` then commit `docs/vendor_history.json`.
- It's encrypted (same passphrase) so it's safe in the public repo.
- Optional: a separate weekly cloud routine running that one command keeps it current.

## Why this design (no Zapier, no upsert)
The script recomputes the WHOLE forecast from live opportunities every run and
overwrites `forecast.json`. Changed, moved, or cancelled jobs are simply correct
on the next run — there is never a stale row to find-and-edit. One file, one
source of truth, recomputed in full.

## First-run setup
Run once with `DEBUG_FIELDS=1 python3 build_forecast.py` to print the real
opportunity field names your account returns, then confirm the date-anchor keys
near the top of `build_forecast.py` match (prep start, out, return start, return
due). Lock them down to your actual keys and remove the fallback guessing if you
want it strict.
