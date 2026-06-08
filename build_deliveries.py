#!/usr/bin/env python3
"""
Limitless — Truck Deliveries pipeline (single file).

Pulls upcoming Truck Driver assignments from the Lasso Workforce API and writes
docs/deliveries.json for the deliveries dashboard to read. Same posture as
build_forecast.py: recompute the whole picture every run, overwrite the file,
encrypt the output so it can sit on public GitHub Pages.

A "drive" = one schedule entry on a Truck-Driver event_position, plus whoever is
rostered to that position. Each schedule entry (e.g. an outbound block and a
return block) becomes its own row.

Read-only by construction: HTTPS GET only, host-pinned to the Lasso API, and the
only thing written is the LOCAL docs/deliveries.json. It never writes to Lasso.

Env vars required:
  LASSO_API_TOKEN      sent as the `LASSO-APIKEY` header
  FORECAST_PASSPHRASE  shared passphrase; when set, output is AES-256-GCM encrypted
Optional:
  LASSO_API_BASE       default https://limitless.lasso.io/api/v1
  DELIVERIES_DAYS      how many days forward to show (default 14)
  TRUCK_POSITION_IDS   comma list of driver position ids (default "31888,31992"
                       = Truck Driver, Van Driver)
"""

import os, sys, json, base64, hashlib, datetime as dt
from collections import defaultdict
import urllib.request, urllib.parse, urllib.error

TOKEN     = os.environ.get("LASSO_API_TOKEN", "")
BASE      = os.environ.get("LASSO_API_BASE", "https://limitless.lasso.io/api/v1").rstrip("/")
DAYS      = int(os.environ.get("DELIVERIES_DAYS", "14"))
TRUCK_IDS = {int(x) for x in os.environ.get("TRUCK_POSITION_IDS", "31888,31992").split(",") if x.strip()}
PASSPHRASE   = os.environ.get("FORECAST_PASSPHRASE", "")
PBKDF2_ITERS = 200_000
OUT = os.path.join(os.path.dirname(__file__), "docs", "deliveries.json")
# Click-through to the event's crew page in the Lasso app. `{code}` is the event
# code (e.g. LMTLS-E019896), `{id}` the numeric id — both available for the
# template. Override LASSO_EVENT_URL if your Lasso route differs.
EVENT_URL = os.environ.get("LASSO_EVENT_URL", "https://limitless.lasso.io/next/events/{code}/crew")

# --- Read-only by construction (mirrors build_forecast.py) ---
ALLOWED_HOST   = urllib.parse.urlsplit(BASE).netloc
ALLOWED_SCHEME = "https"


def get(path, params=None):
    if not TOKEN:
        sys.exit("Missing LASSO_API_TOKEN env var.")
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != ALLOWED_SCHEME or parts.netloc != ALLOWED_HOST:
        sys.exit(f"Refusing request to {url!r}; token only travels TLS to {ALLOWED_HOST}.")
    req = urllib.request.Request(url, method="GET")  # GET only — never a write
    req.add_header("LASSO-APIKEY", TOKEN)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.exit(f"Lasso API {e.code} on {path}: {e.read().decode('utf-8','replace')[:500]}")


def pages(path, params=None, page_size=200, cap=60):
    """Yield every record across all pages (limit/offset pagination)."""
    off = 0
    for _ in range(cap):
        p = dict(params or {}); p.update(limit=page_size, offset=off)
        d = get(path, p)
        rows = d.get("results", []) if isinstance(d, dict) else d
        for x in rows:
            yield x
        if not (isinstance(d, dict) and d.get("next")):
            return
        off += page_size


def encrypt_envelope(plaintext, passphrase):
    """AES-256-GCM with a PBKDF2-HMAC-SHA256 key — decrypts in-browser via Web
    Crypto, identical envelope to build_forecast.py so the same passphrase works."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt = os.urandom(16)
    key  = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, PBKDF2_ITERS, dklen=32)
    iv   = os.urandom(12)
    ct   = AESGCM(key).encrypt(iv, plaintext, None)
    b64  = lambda b: base64.b64encode(b).decode("ascii")
    return {"v": 1, "kdf": "PBKDF2-HMAC-SHA256", "hash": "SHA-256",
            "iter": PBKDF2_ITERS, "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}


def main():
    today = dt.date.today()
    end   = today + dt.timedelta(days=DAYS - 1)
    today_s, end_s = today.isoformat(), end.isoformat()

    # 1) Driver event_positions (schedule_entries ride along inline).
    driver_eps = [ep for ep in pages("/event_positions") if ep.get("position") in TRUCK_IDS]

    # 2) Roster: event_position -> assigned crew (drop 'removed' swaps).
    roster = defaultdict(list)
    for r in pages("/event_roster_positions"):
        if r.get("status") != "removed":
            roster[r.get("event_position")].append(r)

    # 3) Lookups, fetched once.
    crew = {c["id"]: {"name": f"{c.get('first_name','')} {c.get('last_name','')}".strip(),
                      "phone": c.get("phone")} for c in pages("/crew")}

    # 4) Build the drive rows, then resolve only the events/venues we actually need.
    drives, need_events = [], set()
    for ep in driver_eps:
        for se in (ep.get("schedule_entries") or []):
            d = se.get("date") or ""
            if today_s <= d <= end_s:
                need_events.add(ep.get("event"))
                drives.append({"ep": ep, "se": se})

    events, venues = {}, {}
    for eid in need_events:
        ev = get(f"/events/{eid}")
        events[eid] = ev
        vid = ev.get("venue")
        if vid and vid not in venues:
            venues[vid] = get(f"/venues/{vid}")

    rows = []
    for it in drives:
        ep, se = it["ep"], it["se"]
        ev = events.get(ep.get("event"), {})
        ven = venues.get(ev.get("venue"), {})
        drivers = [{"name": crew.get(r.get("crew"), {}).get("name") or "Unknown",
                    "phone": crew.get(r.get("crew"), {}).get("phone"),
                    "status": r.get("status")}
                   for r in roster.get(ep.get("id"), [])]
        drivers.sort(key=lambda x: (x["status"] != "approved", x["name"]))
        rows.append({
            "date": se.get("date"),
            "start": (se.get("start_time") or "")[:5],
            "end": (se.get("end_time") or "")[:5],
            "utc_start": se.get("utc_start"),
            "type": se.get("type"),
            "label": ep.get("label"),
            "event_code": ev.get("code"),
            "event_name": ev.get("name"),
            "event_id": ev.get("id"),
            "event_url": EVENT_URL.format(code=ev.get("code"), id=ev.get("id")) if ev.get("code") else None,
            "venue_name": ven.get("name"),
            "venue_city": ven.get("locality"),
            "venue_region": ven.get("region"),
            "drivers": drivers,
        })

    rows.sort(key=lambda r: (r["date"] or "", r["start"] or ""))

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "horizon_days": DAYS,
        "window": {"start": today_s, "end": end_s},
        "drives": rows,
    }
    raw = json.dumps(payload, indent=2).encode("utf-8")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    if PASSPHRASE:
        with open(OUT, "w") as f:
            json.dump(encrypt_envelope(raw, PASSPHRASE), f, indent=2)
        print(f"Wrote ENCRYPTED {OUT}: {len(rows)} drives over {DAYS} days (AES-256-GCM)")
    else:
        with open(OUT, "wb") as f:
            f.write(raw)
        print(f"Wrote PLAINTEXT {OUT}: {len(rows)} drives over {DAYS} days")


if __name__ == "__main__":
    main()
