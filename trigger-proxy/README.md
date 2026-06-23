# Re-sync trigger proxy

Makes the dashboard's **Re-sync** button pull fresh data straight from RMS/Lasso
and republish — in ~1–2 minutes — instead of only re-fetching the last published
file. The button can't do this directly because the page is public and the RMS
token is a secret, so a click is routed through this tiny proxy.

```
Re-sync click ─▶ Cloudflare Worker (holds GitHub token)
              └─▶ GitHub repository_dispatch
                  └─▶ .github/workflows/rebuild.yml  (runs the two build scripts)
                      └─▶ commit docs/*.json ─▶ Pages redeploys ─▶ page reloads
```

## One-time setup

**1. Add the build secrets to the repo** (Settings ▸ Secrets and variables ▸ Actions ▸ *New repository secret*). Same values as the claude.ai routine's env:
- `CURRENT_RMS_TOKEN`
- `CURRENT_RMS_SUBDOMAIN`  (`limitless`)
- `FORECAST_PASSPHRASE`
- `LASSO_API_TOKEN`

**2. Test the workflow alone** — Actions tab ▸ *rebuild-forecast* ▸ **Run workflow**. It should commit a fresh `forecast: rebuild … [on-demand]` and the dashboard should show a new timestamp. (At this point you already have a working manual rebuild, just not wired to the button yet.)

**3. Create a GitHub token for the Worker** — a **fine-grained PAT**, *Resource owner* = the org, *Only select repositories* = `prep-forecast`, *Repository permissions* ▸ **Contents: Read and write** (this is what `repository_dispatch` needs). Copy it.

**4. Deploy the Worker** (free):
- Cloudflare dashboard ▸ Workers ▸ *Create* ▸ paste `worker.js`, deploy. (Or `npx wrangler deploy`.)
- Settings ▸ *Variables and Secrets*, add encrypted:
  - `GH_TOKEN` = the PAT from step 3
  - `REPO` = `LimitlessLightsandSound/prep-forecast`
  - *(optional)* `ALLOW_ORIGIN` = `https://limitlesslightsandsound.github.io`
- Copy the Worker URL (e.g. `https://prep-rebuild.<you>.workers.dev`).

**5. Wire the button** — give me that Worker URL and I'll point Re-sync at it (it triggers the rebuild, shows "Rebuilding… ~1–2 min", then auto-reloads when the new data lands).

**6. Retire the duplicate** — once this works end-to-end, turn off the claude.ai routine and uncomment the `schedule:` block in `rebuild.yml` so GitHub Actions owns both the timer and the button. Avoids two systems pushing to `master`.

## Why a proxy at all
A static public page can't safely hold a token that can trigger Actions. The
Worker is the smallest possible thing that can: it holds the token server-side and
exposes exactly one action — "rebuild this dashboard" — which is harmless to call
(the builds are GET-only against the APIs and idempotent).
