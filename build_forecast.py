#!/usr/bin/env python3
"""
Limitless — Prep Workload Forecast pipeline (single file).

Pulls active opportunities + their items from Current RMS, computes prep and
de-prep labor spread across each job's scheduled prep/return days, and writes
docs/forecast.json for the GitHub Pages dashboard to read.

Recomputes the ENTIRE forecast from source every run and overwrites the file —
so there is no append/upsert problem. If a job changes, moves, or is cancelled,
the next run simply reflects the correct current picture. No row editing.

Run every 3 hours via a Claude Code scheduled routine (see .claude/routine.md).

Env vars required:
  CURRENT_RMS_TOKEN      X-AUTH-TOKEN from System Setup > Integrations > API
  CURRENT_RMS_SUBDOMAIN  your subdomain (the X-SUBDOMAIN header value)
Optional:
  FORECAST_DAYS          how many days forward to show (default 14)
  SHIFT_HOURS            hours per shop-hand shift for the headcount math (default 8)
  DEBUG_FIELDS=1         print the raw field names of the first opp and exit
"""

import os, sys, json, base64, hashlib, datetime as dt
from collections import defaultdict
import urllib.request, urllib.parse, urllib.error

API = "https://api.current-rms.com/api/v1"
TOKEN     = os.environ.get("CURRENT_RMS_TOKEN", "")
SUBDOMAIN = os.environ.get("CURRENT_RMS_SUBDOMAIN", "")
DAYS      = int(os.environ.get("FORECAST_DAYS", "14"))
SHIFT     = float(os.environ.get("SHIFT_HOURS", "6"))  # shop shift length (hours)
MIN_HANDS = int(os.environ.get("MIN_SHOP_HANDS", "2"))  # floor on any working day
DEBUG     = os.environ.get("DEBUG_FIELDS") == "1"
# Shared-passphrase encryption: when set, the output is encrypted (AES-256-GCM,
# PBKDF2 key) so it can sit on a PUBLIC host and only decrypt in-browser with the
# passphrase. Unset -> plaintext (local debugging). Never commit the passphrase.
PASSPHRASE   = os.environ.get("FORECAST_PASSPHRASE", "")
PBKDF2_ITERS = 200_000
OUT       = os.path.join(os.path.dirname(__file__), "docs", "forecast.json")

# Candidate field names for the date anchors. Account configs vary, so we try
# each in order and use the first present. DEBUG_FIELDS=1 prints what your
# account actually returns so you can lock these down.
# Locked to this account's real field population (surveyed across active opps):
# prep_starts_at(48) deliver_starts_at(59) setup_starts_at(49) deprep_starts_at(43)
# collect_*(56); load_*/unload_* are unused(0); starts_at/ends_at always present.
PREP_START_KEYS  = ["prep_starts_at", "setup_starts_at", "deliver_starts_at", "starts_at"]
OUT_KEYS         = ["deliver_starts_at", "setup_starts_at", "show_starts_at", "starts_at"]
RETURN_START_KEYS= ["deprep_starts_at", "collect_starts_at", "collect_ends_at", "ends_at"]
RETURN_END_KEYS  = ["deprep_ends_at", "collect_ends_at", "ends_at"]

# --- Safety guardrails: read-only by construction ---
# This tool must NEVER modify Current RMS. It issues HTTPS GET requests only, and
# the sole thing it writes is the LOCAL docs/forecast.json. The checks in req()
# make that tamper-evident: the secret auth token is only ever sent over TLS to
# the official RMS host, and any attempt to issue a non-GET (or carry a body)
# aborts the run instead of mutating your account.
ALLOWED_HOST   = "api.current-rms.com"
ALLOWED_SCHEME = "https"

def req(path, params=None):
    if not TOKEN or not SUBDOMAIN:
        sys.exit("Missing CURRENT_RMS_TOKEN or CURRENT_RMS_SUBDOMAIN env vars.")
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    # Host/scheme pinning: the token header is only ever transmitted to the
    # official RMS host over TLS — never anywhere else, even if API is edited.
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != ALLOWED_SCHEME or parts.hostname != ALLOWED_HOST:
        sys.exit(f"Refusing request: token may only be sent to "
                 f"{ALLOWED_SCHEME}://{ALLOWED_HOST} "
                 f"(got {parts.scheme}://{parts.hostname})")
    # Read-only by construction: explicit GET, never a request body.
    r = urllib.request.Request(url, method="GET", headers={
        "X-AUTH-TOKEN": TOKEN,
        "X-SUBDOMAIN": SUBDOMAIN,
        "Content-Type": "application/json",
    })
    if r.get_method() != "GET" or r.data is not None:
        sys.exit("Refusing non-GET request: this tool is read-only against RMS.")
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # Note: e.read() is the RMS response body, which never contains our token.
        sys.exit(f"API error {e.code} on {path}: {e.read().decode()[:300]}")

def get_all(path, key, params=None, page_size=50, cap_pages=40):
    """Paginate an index endpoint, returning the combined list under `key`."""
    params = dict(params or {})
    params["per_page"] = page_size
    out, page = [], 1
    while page <= cap_pages:
        params["page"] = page
        data = req(path, params)
        chunk = data.get(key, [])
        out.extend(chunk)
        meta = data.get("meta", {})
        total = meta.get("total_row_count")
        if not chunk or (total is not None and len(out) >= total):
            break
        page += 1
    return out

def parse_dt(s):
    if not s: return None
    s = s.replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
            try: return dt.datetime.strptime(s, fmt)
            except ValueError: continue
    return None

def date_only(x):
    return x.date() if isinstance(x, dt.datetime) else x

def first_key(obj, keys):
    for k in keys:
        if obj.get(k):
            return obj[k]
    return None

def days_span(a, b):
    """Inclusive list of date objects from a..b (b defaults to a)."""
    a = date_only(parse_dt(a)) if isinstance(a, str) else date_only(a)
    b = date_only(parse_dt(b)) if isinstance(b, str) else date_only(b)
    if not a: return []
    if not b or b < a: b = a
    out, cur = [], a
    while cur <= b:
        out.append(cur)
        cur += dt.timedelta(days=1)
    return out

def num(v):
    try: return float(v)
    except (TypeError, ValueError): return 0.0

def build_product_labor():
    """Map product_id -> (prep_mins, deprep_mins) from product custom fields.

    The labor minutes live on the PRODUCT (imported via CSV), not on the
    opportunity line item — the item's custom_fields come back empty. So we
    pull all products once and join opportunity items to this map by item_id.
    """
    prods = get_all("/products", "products", page_size=100)
    m = {}
    for p in prods:
        cfs = p.get("custom_fields")
        if isinstance(cfs, dict):
            m[p.get("id")] = (num(cfs.get("prep_labor_mins")),
                              num(cfs.get("deprep_labor_mins")))
    return m

def encrypt_envelope(plaintext, passphrase):
    """AES-256-GCM with a PBKDF2-HMAC-SHA256 key. The envelope decrypts in the
    browser via Web Crypto (importKey PBKDF2 -> deriveKey AES-GCM -> decrypt).
    AES-GCM ciphertext here already includes the 16-byte auth tag appended, which
    is exactly what Web Crypto's decrypt expects."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt = os.urandom(16)
    key  = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt,
                               PBKDF2_ITERS, dklen=32)
    iv   = os.urandom(12)
    ct   = AESGCM(key).encrypt(iv, plaintext, None)
    b64  = lambda b: base64.b64encode(b).decode("ascii")
    return {"v": 1, "kdf": "PBKDF2-HMAC-SHA256", "hash": "SHA-256",
            "iter": PBKDF2_ITERS, "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}

def main():
    today = dt.date.today()
    horizon = today + dt.timedelta(days=DAYS)

    # Active opportunities (default scope already excludes cancelled/dead).
    # Embed items so we don't make an N+1 call per opp.
    opps = get_all("/opportunities", "opportunities",
                   params={"include[]": "opportunity_items"})

    if DEBUG and opps:
        print("=== First opportunity raw keys ===")
        print(json.dumps({k: opps[0][k] for k in list(opps[0].keys())}, default=str, indent=2)[:4000])
        sys.exit(0)

    # Labor minutes live on products; build the lookup once and join by item_id.
    labor = build_product_labor()

    # Per day we split minutes into confirmed (Order) vs at-risk (unconfirmed),
    # so the dashboard can show a committed floor plus an "if quotes confirm" delta.
    # day -> {prep_c, prep_r, deprep_c, deprep_r, jobs:{(oid,name,phase,confirmed):mins}}
    buckets = defaultdict(lambda: {"prep_c": 0.0, "prep_r": 0.0,
                                   "deprep_c": 0.0, "deprep_r": 0.0,
                                   "jobs": defaultdict(float)})

    for opp in opps:
        oid  = opp.get("id")
        name = opp.get("subject") or opp.get("number") or f"Opp {oid}"
        confirmed = (opp.get("state_name") == "Order")  # else quoted/unconfirmed
        items = opp.get("opportunity_items", [])
        if not items:
            # items not embedded for this config — fetch explicitly
            items = get_all("/opportunity_items", "opportunity_items",
                            params={"q[opportunity_id_eq]": opp.get("id")})

        prep_total = deprep_total = 0.0
        for it in items:
            if it.get("item_type") != "Product":
                continue  # groups, services, fees, sub-rentals carry no prep labor
            pl, dl = labor.get(it.get("item_id"), (0.0, 0.0))
            q = num(it.get("quantity", 1))
            prep_total   += pl * q
            deprep_total += dl * q
        if prep_total == 0 and deprep_total == 0:
            continue

        # Prep happens BEFORE the gear goes out, so drop the out/delivery day itself
        # — e.g. an 8am Monday delivery should NOT show prep on Monday. If prep and
        # delivery land on the same day, fall back to the day before delivery so the
        # work still appears somewhere sensible.
        prep_span = days_span(first_key(opp, PREP_START_KEYS), first_key(opp, OUT_KEYS))
        if prep_span:
            prep_days = prep_span[:-1] or [prep_span[-1] - dt.timedelta(days=1)]
        else:
            prep_days = []
        deprep_days = days_span(first_key(opp, RETURN_START_KEYS), first_key(opp, RETURN_END_KEYS))

        if prep_days:
            per = prep_total / len(prep_days)
            for d in prep_days:
                if today <= d <= horizon:
                    buckets[d]["prep_c" if confirmed else "prep_r"] += per
                    buckets[d]["jobs"][(oid, name, "PREP", confirmed)] += per
        if deprep_days:
            per = deprep_total / len(deprep_days)
            for d in deprep_days:
                if today <= d <= horizon:
                    buckets[d]["deprep_c" if confirmed else "deprep_r"] += per
                    buckets[d]["jobs"][(oid, name, "DEPREP", confirmed)] += per

    rows = []
    prep_map = {}   # date -> set of opp ids with PREP that day (for the per-day ticket)
    for d in sorted(buckets):
        b = buckets[d]
        prep_map[d.isoformat()] = {o for (o, n, ph, cf) in b["jobs"] if ph == "PREP"}
        prep_mins   = b["prep_c"] + b["prep_r"]
        deprep_mins = b["deprep_c"] + b["deprep_r"]
        confirmed_mins = b["prep_c"] + b["deprep_c"]
        atrisk_mins    = b["prep_r"] + b["deprep_r"]
        prep_h   = round(prep_mins / 60, 1)
        deprep_h = round(deprep_mins / 60, 1)
        total_h  = round(prep_h + deprep_h, 1)
        # Staffing shows FIRM hands (confirmed work only), floored at MIN_HANDS.
        firm = max(MIN_HANDS, int(-(-confirmed_mins // (SHIFT * 60))))
        # Contingency = extra hands if the day's quotes confirm; at least +1 when
        # any unconfirmed work exists, so logistics sees the potential bump.
        if_all = max(MIN_HANDS, int(-(-(confirmed_mins + atrisk_mins) // (SHIFT * 60))))
        contingency = if_all - firm
        if atrisk_mins > 0 and contingency < 1:
            contingency = 1
        top = sorted(b["jobs"].items(), key=lambda kv: kv[1], reverse=True)[:4]
        jobs = [{"id": oid, "name": n, "phase": ph, "confirmed": conf, "hours": round(m / 60, 1)}
                for (oid, n, ph, conf), m in top]
        rows.append({
            "date": d.isoformat(),
            "prep_hrs": prep_h,
            "deprep_hrs": deprep_h,
            "total_hrs": total_h,
            "confirmed_hrs": round(confirmed_mins / 60, 1),
            "atrisk_hrs": round(atrisk_mins / 60, 1),
            "shop_hands": firm,
            "contingency_hands": contingency,
            "jobs": jobs,
        })

    # Risk scan: opportunities NOT yet confirmed (state_name != "Order") whose gear
    # is due out within the window. Logistics should escalate these to accounts —
    # the shop may already be planning prep for jobs that aren't locked in.
    risk = []
    for opp in opps:
        state = opp.get("state_name")
        if state == "Order":
            continue  # confirmed — not a risk
        out = first_key(opp, OUT_KEYS)
        od = date_only(parse_dt(out)) if out else None
        if not (od and today <= od <= horizon):
            continue
        risk.append({
            "id": opp.get("id"),
            "name": opp.get("subject") or opp.get("number") or f"Opp {opp.get('id')}",
            "state": state,                       # e.g. "Quotation" / "Draft"
            "status": opp.get("status_name"),     # e.g. "Provisional" / "Open"
            "out_date": od.isoformat(),
            "value": round(num(opp.get("charge_total")), 0),
        })
    risk.sort(key=lambda r: (r["out_date"], -r["value"]))

    # Logistics: sub-rentals (with supplier) and shortages due out in the window.
    # The vendor lives on the sub-rent ASSET (item_assets on the opportunity-item
    # nested route), not the line itself — so we fetch nested items only for opps
    # that actually have sub-rentals, and resolve supplier_id -> member name.
    _member = {}
    def supplier_name(sid):
        if not sid:
            return None
        if sid not in _member:
            try:
                nm = (req(f"/members/{sid}").get("member", {}) or {}).get("name") or ""
            except SystemExit:
                nm = ""
            _member[sid] = nm.replace("(SUPPLIER)", "").strip() or None
        return _member[sid]

    sub_rentals, shortages = [], []
    for opp in opps:
        out = first_key(opp, OUT_KEYS)
        od = date_only(parse_dt(out)) if out else None
        if not (od and today <= od <= horizon):
            continue
        oid  = opp.get("id")
        name = opp.get("subject") or opp.get("number") or f"Opp {oid}"
        items = opp.get("opportunity_items", [])
        short_lines = [it for it in items if it.get("has_shortage")]
        if short_lines:
            shortages.append({
                "id": oid, "name": name, "out_date": od.isoformat(),
                "count": len(short_lines),
                "items": [it.get("name") for it in short_lines[:8]],
            })
        if any(it.get("sub_rent") for it in items):
            nested = get_all(f"/opportunities/{oid}/opportunity_items",
                             "opportunity_items", page_size=100)
            for it in nested:
                for a in (it.get("item_assets") or []):
                    if a.get("sub_rent"):
                        sub_rentals.append({
                            "id": oid, "name": name,
                            "item": it.get("name"), "qty": num(a.get("quantity")),
                            "supplier": supplier_name(a.get("supplier_id")),
                        })
    sub_rentals.sort(key=lambda s: (s["name"], s["item"]))

    # Per-prep-day logistics ticket: for each prep day, the jobs prepping that day
    # that have sub-rentals (vendor / unallocated) or shortages. No pickup/return
    # direction is implied — vendors may deliver or be collected.
    names = {o.get("id"): (o.get("subject") or o.get("number") or f"Opp {o.get('id')}")
             for o in opps}
    sub_by_job = {}
    for s in sub_rentals:
        e = sub_by_job.setdefault(s["id"], {"vendors": set(), "unalloc": False})
        if s["supplier"]:
            e["vendors"].add(s["supplier"])
        else:
            e["unalloc"] = True
    short_by_job = {s["id"]: s["count"] for s in shortages}
    for row in rows:
        # Per-day ticket: jobs prepping that day that have sub-rentals OR shortages.
        # Vendors show as chips; a shortage count rides as a red badge by the name.
        logi = []
        for joid in prep_map.get(row["date"], set()):
            v = sub_by_job.get(joid)
            sc = short_by_job.get(joid, 0)
            if not v and not sc:
                continue
            logi.append({
                "id": joid, "name": names.get(joid, f"Opp {joid}"),
                "vendors": sorted(v["vendors"]) if v else [],
                "unalloc": bool(v and v["unalloc"]),
                "shortage": sc,
            })
        logi.sort(key=lambda x: x["name"])
        row["logi"] = logi
    shortages.sort(key=lambda s: s["out_date"])

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "horizon_days": DAYS,
        "shift_hours": SHIFT,
        "min_hands": MIN_HANDS,
        "rms_base": f"https://{SUBDOMAIN}.current-rms.com",  # opp link = rms_base/opportunities/<id>
        "days": rows,
        "risk": risk,
        "sub_rentals": sub_rentals,
        "shortages": shortages,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    raw = json.dumps(payload, indent=2).encode("utf-8")
    if PASSPHRASE:
        with open(OUT, "w") as f:
            json.dump(encrypt_envelope(raw, PASSPHRASE), f, indent=2)
        print(f"Wrote ENCRYPTED {OUT}: {len(rows)} days (AES-256-GCM)")
    else:
        with open(OUT, "wb") as f:
            f.write(raw)
        print(f"Wrote PLAINTEXT {OUT}: {len(rows)} days "
              f"(no FORECAST_PASSPHRASE set) — do NOT publish this")

if __name__ == "__main__":
    main()
