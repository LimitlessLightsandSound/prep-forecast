# Governance & Guardrails — Prep Forecast

This tool reads from Current RMS to build a forecast. It must **never** modify
RMS, and it handles a live API token. The rules below are enforced in code where
possible and by process otherwise.

## 1. Read-only contract (code-enforced)
- `build_forecast.py` issues **HTTPS GET only**. `req()` sets `method="GET"`
  explicitly and aborts if any request carries a body or a non-GET verb.
- **Host pinning:** the auth token header is only ever transmitted over TLS to
  `api.current-rms.com`. If the `API` constant is ever changed to another host,
  the run aborts rather than sending the token elsewhere.
- The only thing the script writes is the **local** file `docs/forecast.json`.
  No POST/PUT/PATCH/DELETE exists anywhere in the codebase.
- The dashboard (`docs/index.html`) only `fetch()`es the local JSON. It never
  writes back.

## 2. Secret handling
- Credentials live **only** in `.env` (gitignored, `chmod 600`). Never in code,
  never committed, never pasted into chat or commit messages.
- The token is never printed or logged. `DEBUG_FIELDS=1` prints opportunity
  field names only; API error output is the RMS response body, which does not
  contain the token.
- If the token is ever exposed, **rotate it** in RMS (System Setup >
  Integrations > API) immediately.

## 3. Least privilege
- Prefer an API token scoped to read-only / the minimum needed, if RMS supports
  per-token scopes. The code never needs write access — a read-only token makes
  the read-only contract enforced on the server side too.

## 4. Pre-run gate (process)
1. `.env` filled with real values.
2. `DEBUG_FIELDS=1` run first to confirm field mapping — this exits without
   writing or modifying anything.
3. Only then a real run. A real run still only writes the local JSON.

## 5. Data exposure decisions (on record)
- **2026-06-05, Dash — initial:** accepted publishing client/job names on a
  public GitHub Pages dashboard.
- **2026-06-05, Dash — reversed:** repo set private (free-tier Pages then can't
  serve, so dashboard went offline). Superseded same day by the decision below.
- **2026-06-05, Dash — FINAL: encrypt-at-rest, public host.** The published
  `docs/forecast.json` is encrypted with **AES-256-GCM**, key derived from a
  shared passphrase via **PBKDF2-HMAC-SHA256 (200k iters)**. The dashboard
  prompts for the passphrase and decrypts in-browser (Web Crypto). The repo can
  be public and served by free GitHub Pages because the data is never published
  in the clear — fetching `forecast.json` directly yields only `{salt,iv,ct}`.
  - Confirmed safe for browser: Python-encrypted envelope verified to decrypt
    under WebCrypto and to reject a wrong passphrase.
  - **Threat model / limits:** one *shared* passphrase, not per-user logins; no
    revocation except rotating the passphrase (change `FORECAST_PASSPHRASE` and
    the routine re-encrypts on next run). Strength rests on passphrase entropy —
    use a strong one, since the ciphertext is public and brute-forceable offline.
  - **History scrub required before going public:** earlier commits of
    `docs/forecast.json` hold PLAINTEXT client names. Git history must be reset/
    scrubbed before the repo is made public, or that history leaks.
  - `FORECAST_PASSPHRASE` is a secret: lives in `.env` (local) and the routine's
    cloud env only. Never commit it.

## 6. Change control
- Any edit to `req()` or the `API` constant must preserve the GET-only and
  host-pinning guards. They fail closed (abort) by design — do not loosen them
  to "make a write work"; this tool has no business writing to RMS.
