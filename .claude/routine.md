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
```
Repo access: grant the routine `LimitlessLightsandSound/prep-forecast` and enable
**"Allow unrestricted branch pushes"** so it can push to `master`.

### Cloud-environment settings that are easy to get wrong (verified working)
- **Network access = Custom**, with `api.current-rms.com` in the allowlist, AND
  keep "include default package managers" checked. Trusted/None blocks the RMS
  call with `403 Host not in allowlist`; Custom+RMS is the least-privilege fit.
- **Setup script = `pip install cryptography`** — NOT `pip install -r
  requirements.txt`. The setup script runs before the repo is checked out, so the
  requirements file isn't present yet; installing the package by name avoids the
  `No such file or directory: 'requirements.txt'` failure.
- GitHub connection is separate from the App install: the claude.ai account must
  be linked to the GitHub identity (claude.ai/settings/connected-accounts) before
  the repo appears in the routine's repository picker.

### Routine prompt
1. In the `prep-forecast` repo on branch `master`, install deps and run the build:
   `pip install -r requirements.txt && python3 build_forecast.py`
   (`CURRENT_RMS_TOKEN`, `CURRENT_RMS_SUBDOMAIN`, `FORECAST_PASSPHRASE` are already
   in the environment.) With the passphrase set, `docs/forecast.json` is written
   ENCRYPTED (AES-256-GCM) — safe to publish.
2. Commit `docs/forecast.json` with message `forecast: <current UTC timestamp>`
   and push to `master`. (The ciphertext changes every run by design — a random
   salt/IV each time — so expect a commit on every run.)
3. Do not modify any other file. The script is read-only against Current RMS.

GitHub Pages (PUBLIC repo, branch `master`, `/docs`) serves the dashboard at
https://limitlesslightsandsound.github.io/prep-forecast/ and rebuilds on each
push. The published data is encrypted; the page prompts for the shared passphrase
and decrypts in-browser. Free plan is fine — no private Pages needed because the
data is never published in the clear.

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
