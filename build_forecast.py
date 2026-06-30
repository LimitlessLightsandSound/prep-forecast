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
# LOGISTICS-PORT (Route B): when these are set, ALSO publish the PLAINTEXT payload to the
# Limitless Pipeline app's ingest endpoint, which writes it to the private Firestore doc
# `logistics/forecast`. This is independent of the encrypted GitHub-Pages write above (both run
# during the migration). It POSTs to the APP — not RMS — with its own shared secret, so it never
# touches the read-only RMS token / host-pin contract in req().
INGEST_URL    = os.environ.get("LOGISTICS_INGEST_URL", "")
INGEST_SECRET = os.environ.get("LOGISTICS_INGEST_SECRET", "")

# Candidate field names for the date anchors. Account configs vary, so we try
# each in order and use the first present. DEBUG_FIELDS=1 prints what your
# account actually returns so you can lock these down.
# Locked to this account's real field population (surveyed across active opps):
# prep_starts_at(48) deliver_starts_at(59) setup_starts_at(49) deprep_starts_at(43)
# collect_*(56); load_*/unload_* are unused(0); starts_at/ends_at always present.
PREP_START_KEYS  = ["prep_starts_at", "setup_starts_at", "deliver_starts_at", "starts_at"]
OUT_KEYS         = ["deliver_starts_at", "setup_starts_at", "show_starts_at", "starts_at"]
# De-prep labor follows the DE-PREP WINDOW set in Current RMS — deliberately NOT
# the truck collection/return time. RMS stores collection at the venue, not when
# the gear is back at the shop, so collect/ends_at would land de-prep on the wrong
# day for late returns. The shop controls timing by setting the de-prep window in
# Current; an opp with no de-prep window simply shows no de-prep labor here.
RETURN_START_KEYS= ["deprep_starts_at"]
RETURN_END_KEYS  = ["deprep_ends_at"]

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

def build_product_meta():
    """Map product_id -> (prep_mins, deprep_mins) and product_id -> product_group_id.

    The labor minutes live on the PRODUCT (imported via CSV), not on the
    opportunity line item — the item's custom_fields come back empty. So we
    pull all products once and join opportunity items to this map by item_id.
    The product group rides along (one fetch) so shortages can be grouped by it.
    """
    prods = get_all("/products", "products", page_size=100)
    labor, group = {}, {}
    for p in prods:
        pid = p.get("id")
        cfs = p.get("custom_fields")
        if isinstance(cfs, dict):
            labor[pid] = (num(cfs.get("prep_labor_mins")),
                          num(cfs.get("deprep_labor_mins")))
        group[pid] = p.get("product_group_id")
    return labor, group

def build_group_names():
    """product_group_id -> group name (e.g. POWER, CABLE, RIGGING). One small fetch."""
    return {g.get("id"): g.get("name")
            for g in get_all("/product_groups", "product_groups", page_size=100)}

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

def decrypt_envelope(env, passphrase):
    """Inverse of encrypt_envelope — used to read the (committed, encrypted)
    vendor-history cache back in. Same PBKDF2 + AES-256-GCM as the browser."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"),
                              base64.b64decode(env["salt"]),
                              int(env.get("iter", PBKDF2_ITERS)), dklen=32)
    pt  = AESGCM(key).decrypt(base64.b64decode(env["iv"]),
                              base64.b64decode(env["ct"]), None)
    return json.loads(pt.decode("utf-8"))

VENDOR_HISTORY = os.path.join(os.path.dirname(__file__), "docs", "vendor_history.json")

def load_vendor_history():
    """Read the vendor-history cache (built occasionally by build_vendor_history.py).
    Handles both the encrypted envelope and plaintext; returns {} if absent/unreadable
    so the forecast still runs without suggestions."""
    try:
        with open(VENDOR_HISTORY) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if isinstance(data, dict) and data.get("ct") and data.get("kdf"):
        if not PASSPHRASE:
            return {}
        try:
            data = decrypt_envelope(data, PASSPHRASE)
        except Exception:
            return {}
    return data if isinstance(data, dict) else {}

def publish_to_app(feed, payload):
    """LOGISTICS-PORT: POST a plaintext payload to the app's ingest endpoint so it lands in Firestore
    `logistics/<feed>` for the in-app dashboard. `feed` is 'forecast' | 'deliveries' | 'vendorHistory'.
    No-op unless both LOGISTICS_INGEST_URL and LOGISTICS_INGEST_SECRET are set. Best-effort: a publish
    failure is logged but never fails the run. Targets the APP, never RMS — carries the ingest secret."""
    if not INGEST_URL or not INGEST_SECRET:
        return
    body = json.dumps({"feed": feed, "payload": payload}).encode("utf-8")
    req = urllib.request.Request(INGEST_URL, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "x-logistics-secret": INGEST_SECRET,
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"Published {feed} to app ingest: HTTP {resp.status} {resp.read().decode()[:200]}")
    except urllib.error.HTTPError as e:
        print(f"App ingest rejected {feed}: HTTP {e.code} {e.read().decode()[:200]}")
    except Exception as e:  # noqa: BLE001 — publish is best-effort, never fail the build on it
        print(f"App ingest publish failed (non-fatal): {e}")


def main():
    today = dt.date.today()
    horizon = today + dt.timedelta(days=DAYS)

    # Active opportunities (default scope already excludes cancelled/dead).
    # We still ask RMS to embed opportunity_items + owner, but as of ~2026-06-12
    # RMS no longer embeds opportunity_items (owner still embeds), so items are
    # hydrated per-opp below. Keeping the include[] means the fast embedded path
    # resumes automatically if RMS restores it.
    # NB: once any include[] is set, RMS only returns the associations you ask
    # for — so owner (the account manager) must be requested explicitly too.
    opps = get_all("/opportunities", "opportunities",
                   params={"include[]": ["opportunity_items", "owner"]})

    if DEBUG and opps:
        print("=== First opportunity raw keys ===")
        print(json.dumps({k: opps[0][k] for k in list(opps[0].keys())}, default=str, indent=2)[:4000])
        sys.exit(0)

    # RMS stopped embedding opportunity_items via include[] (and removed the
    # top-level /opportunity_items index, which now 404s "No route matches"), so
    # hydrate each opp's items once from the per-opportunity nested route. Every
    # downstream loop then reads opp["opportunity_items"] inline as before. If RMS
    # ever restores include[] embedding, the guard skips the call automatically.
    for opp in opps:
        if not opp.get("opportunity_items"):
            opp["opportunity_items"] = get_all(
                f"/opportunities/{opp.get('id')}/opportunity_items",
                "opportunity_items", page_size=100)

    # Labor minutes live on products; build the lookup once and join by item_id.
    # The product-group lookup rides along for grouping shortages by group.
    labor, prod_group = build_product_meta()
    group_names = build_group_names()
    def group_name(pid):
        return group_names.get(prod_group.get(pid)) or "OTHER"

    # Per day we split minutes into confirmed (Order) vs at-risk (unconfirmed),
    # so the dashboard can show a committed floor plus an "if quotes confirm" delta.
    # day -> {prep_c, prep_r, deprep_c, deprep_r, jobs:{(oid,name,phase,confirmed):mins}}
    buckets = defaultdict(lambda: {"prep_c": 0.0, "prep_r": 0.0,
                                   "deprep_c": 0.0, "deprep_r": 0.0,
                                   "jobs": defaultdict(float)})
    # Account manager per opp (RMS "owner" = assigned user) so shop techs know
    # who to ask about a job.
    managers = {}
    # Shows with de-prep labor due back in-window but no de-prep window set in RMS.
    missing_deprep = []

    for opp in opps:
        oid  = opp.get("id")
        name = opp.get("subject") or opp.get("number") or f"Opp {oid}"
        confirmed = (opp.get("state_name") == "Order")  # else quoted/unconfirmed
        owner = opp.get("owner")
        managers[oid] = owner.get("name") if isinstance(owner, dict) else None
        items = opp.get("opportunity_items", [])

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

        # Has de-prep labor but no de-prep window set in Current, yet gear is due
        # back within the window — flag it so the shop sets the window (instead of
        # the de-prep labor silently going missing from the staffing picture).
        if deprep_total > 0 and not deprep_days:
            back = date_only(parse_dt(first_key(opp, ["collect_starts_at", "collect_ends_at", "ends_at"])))
            if back and today <= back <= horizon:
                missing_deprep.append({
                    "id": oid, "name": name, "manager": managers.get(oid),
                    "back_date": back.isoformat(),
                    "hours": round(deprep_total / 60, 1),
                })

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
        jobs = [{"id": oid, "name": n, "phase": ph, "confirmed": conf, "hours": round(m / 60, 1),
                 "manager": managers.get(oid)}
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
            "manager": managers.get(opp.get("id")),
        })
    risk.sort(key=lambda r: (r["out_date"], -r["value"]))

    # Logistics: sub-rentals (with supplier) and shortages due out in the window.
    # The vendor lives on the sub-rent ASSET (item_assets on the opportunity-item
    # nested route), not the line itself — so we fetch nested items only for opps
    # that actually have sub-rentals, and resolve supplier_id -> member name.
    _member = {}   # sid -> {"name": str|None, "address": str|None}
    def _fmt_address(m):
        """Best-effort one-line address from a Current RMS member/organisation.
        Tries the primary address object, then the first listed address; returns
        None if nothing usable so the UI can simply omit it. Defensive about the
        exact field names since account configs vary."""
        cand = m.get("primary_address") or {}
        if not cand:
            addrs = m.get("addresses") or []
            cand = addrs[0] if addrs else {}
        if not isinstance(cand, dict):
            return None
        full = (cand.get("full_address") or "").strip()
        if full:
            return " ".join(full.split())
        parts = [cand.get("street"), cand.get("city"),
                 cand.get("county") or cand.get("state"),
                 cand.get("postcode") or cand.get("zip"),
                 cand.get("country_name")]
        line = ", ".join(str(p).strip() for p in parts if p and str(p).strip())
        return line or None
    def _member_info(sid):
        if sid not in _member:
            info = {"name": None, "address": None}
            try:
                m = req(f"/members/{sid}").get("member", {}) or {}
                info["name"] = (m.get("name") or "").replace("(SUPPLIER)", "").strip() or None
                info["address"] = _fmt_address(m)
            except SystemExit:
                pass
            _member[sid] = info
        return _member[sid]
    def supplier_name(sid):
        return _member_info(sid)["name"] if sid else None

    # --- Real shortage QUANTITIES ------------------------------------------
    # RMS flags a line `has_shortage` but never says HOW MANY are short, and the
    # public API exposes no availability figure. The line quantity is the full
    # booked amount, which massively over-states the shortage. We reconstruct the
    # shortfall the way RMS's Availability view does, over the job's window:
    #     supply = owned stock − quarantine − flagged-unavailable
    #     short  = min(qty this job booked, max(0, concurrent_demand − supply))
    # concurrent_demand = every booking of that product whose window overlaps
    # this job's AND that actually draws down owned stock. QUARANTINE matters and
    # is easy to miss: damaged/lost gear sits in an open-ended quarantine record
    # that does NOT appear in the stock level's quantity_unavailable, yet RMS
    # removes it from availability — so ignoring it under-counts the shortage
    # (e.g. 3 quarantined True1 cables turn a "5 short" into the correct "8
    # short"). Capping at the job's own quantity keeps the per-job number honest.
    #
    # Two kinds of booking do NOT consume owned stock and must be excluded from
    # concurrent_demand, or shortages over-state badly (this over-stated DreamCon
    # KS28 as 8 when RMS shows 4):
    #   • sub-rented lines (`sub_rent`) — that qty is sourced from a vendor, not
    #     pulled from the shelf, so it doesn't reduce availability.
    #   • Provisional quotes (`status_name == "Provisional"`) — RMS only reserves
    #     stock for Orders and *Reserved* quotes; provisional/tentative quotes
    #     hold nothing, so RMS's availability ignores them. (Reserved quotes DO
    #     hold stock and are kept.)
    prod_demand = defaultdict(list)   # product_id -> [(start, end, qty)]
    for opp in opps:
        if opp.get("status_name") == "Provisional":
            continue   # tentative quote: reserves no stock in RMS
        for it in opp.get("opportunity_items", []):
            if it.get("item_type") != "Product" or it.get("sub_rent"):
                continue   # sub-rented qty comes from a vendor, not owned stock
            s, e = parse_dt(it.get("starts_at")), parse_dt(it.get("ends_at"))
            if s and e:
                prod_demand[it.get("item_id")].append((s, e, num(it.get("quantity"))))

    _held = {}
    def stock_held(pid):
        """Units the shop physically holds, net of flagged-unavailable stock."""
        if pid not in _held:
            rows = get_all("/stock_levels", "stock_levels",
                           params={"q[item_id_eq]": pid}, page_size=100)
            held = sum(num(r.get("quantity_held")) for r in rows)
            unav = sum(num(r.get("quantity_unavailable")) for r in rows)
            _held[pid] = max(0.0, held - unav)
        return _held[pid]

    _quar = {}
    def quarantined(pid, start, end):
        """Units in quarantine (damaged/lost/maintenance hold) overlapping [start,end)."""
        if pid not in _quar:
            rows = get_all("/quarantines", "quarantines",
                           params={"q[item_id_eq]": pid, "q[active_eq]": "true"},
                           page_size=100)
            _quar[pid] = [(parse_dt(r.get("starts_at")), parse_dt(r.get("ends_at")),
                           num(r.get("quantity")) - num(r.get("quantity_out")))
                          for r in rows if r.get("active")]
        tot = 0.0
        for (s, e, q) in _quar[pid]:
            if q > 0 and s and e and s < end and start < e:
                tot += q
        return tot

    def supply(pid, start, end):
        """Units of a product genuinely available to rent across [start,end)."""
        return stock_held(pid) - quarantined(pid, start, end)

    def concurrent_demand(pid, start, end):
        """Total qty of a product booked across all active opps overlapping [start,end)."""
        return sum(q for (s, e, q) in prod_demand.get(pid, [])
                   if s < end and start < e)

    def job_dates(opp):
        """(pickup, return) for a job: first prep day and de-prep day, ISO or None."""
        prep_span = days_span(first_key(opp, PREP_START_KEYS), first_key(opp, OUT_KEYS))
        prep_days = (prep_span[:-1] or [prep_span[-1] - dt.timedelta(days=1)]) if prep_span else []
        deprep_days = days_span(first_key(opp, RETURN_START_KEYS), first_key(opp, RETURN_END_KEYS))
        return (prep_days[0].isoformat() if prep_days else None,
                deprep_days[0].isoformat() if deprep_days else None)

    sub_rentals, shortages = [], []
    for opp in opps:
        out = first_key(opp, OUT_KEYS)
        od = date_only(parse_dt(out)) if out else None
        if not (od and today <= od <= horizon):
            continue
        oid  = opp.get("id")
        name = opp.get("subject") or opp.get("number") or f"Opp {oid}"
        items = opp.get("opportunity_items", [])
        # Only Products carry stock (and thus shortages); collapse repeat lines of
        # the same product into one so a product short across two lines shows once.
        short_lines = [it for it in items
                       if it.get("has_shortage") and it.get("item_type") == "Product"]
        # Text items are free-text lines not linked to a catalog product, so RMS
        # can't compute availability and NEVER flags them short — surface them too
        # so gear booked as text isn't silently missed. (qty 0 = a note/heading.)
        text_lines = [it for it in items
                      if it.get("item_type") == "TextItem" and num(it.get("quantity")) > 0]
        if short_lines or text_lines:
            short_items = []
            if short_lines:
                by_prod = {}   # product_id -> {name, qty, start, end}
                for it in short_lines:
                    pid = it.get("item_id")
                    s, e = parse_dt(it.get("starts_at")), parse_dt(it.get("ends_at"))
                    g = by_prod.setdefault(pid, {"name": it.get("name"), "qty": 0.0,
                                                 "start": s, "end": e})
                    g["qty"] += num(it.get("quantity"))
                    if s and (g["start"] is None or s < g["start"]): g["start"] = s
                    if e and (g["end"]  is None or e > g["end"]):    g["end"]  = e
                for pid, g in by_prod.items():
                    if g["start"] and g["end"]:
                        over = (concurrent_demand(pid, g["start"], g["end"])
                                - supply(pid, g["start"], g["end"]))
                        short = min(g["qty"], max(0.0, over))
                    else:
                        short = g["qty"]
                    # RMS flagged this product short; never hide that — floor at 1.
                    short = max(1, round(short))
                    short_items.append({"name": g["name"], "qty": short,
                                        "group": group_name(pid), "pid": pid})
                # Organise by product group (POWER, CABLE, ...), then biggest
                # shortage first within a group, so it reads like a pull sheet.
                short_items.sort(key=lambda x: (x["group"], -x["qty"], x["name"]))
            # Aggregate text items by name (combine repeat lines).
            tagg = {}
            for it in text_lines:
                nm = it.get("name") or "—"
                tagg[nm] = tagg.get(nm, 0.0) + num(it.get("quantity"))
            text_items = [{"name": nm, "qty": q} for nm, q in sorted(tagg.items())]
            pu, rt = job_dates(opp)   # pickup = first prep day, return = de-prep day
            shortages.append({
                "id": oid, "name": name, "number": opp.get("number"),
                "out_date": od.isoformat(),
                "pickup_date": pu, "return_date": rt,
                "count": len(short_items),
                "text_count": len(text_items),
                "items": short_items,        # full list — no roll-off
                "text_items": text_items,    # RMS-untracked free-text lines
            })
        if any(it.get("sub_rent") for it in items):
            nested = get_all(f"/opportunities/{oid}/opportunity_items",
                             "opportunity_items", page_size=100)
            pu, rt = job_dates(opp)
            # Combine repeat sub-rent bookings of the same item+supplier into one
            # line: sum the quantity and keep the parts so the UI can show "10+2".
            agg = {}   # (item, supplier) -> {qty, parts}
            for it in nested:
                for a in (it.get("item_assets") or []):
                    if a.get("sub_rent"):
                        key = (it.get("name"), supplier_name(a.get("supplier_id")))
                        e = agg.setdefault(key, {"qty": 0.0, "parts": []})
                        qa = num(a.get("quantity"))
                        e["qty"] += qa
                        e["parts"].append(qa)
            for (itname, sup), e in agg.items():
                parts = sorted(e["parts"], reverse=True)
                sub_rentals.append({
                    "id": oid, "name": name, "item": itname, "qty": e["qty"],
                    "parts": parts if len(parts) > 1 else None,
                    "supplier": sup, "pickup_date": pu, "return_date": rt,
                })
    sub_rentals.sort(key=lambda s: (s["name"], s["item"]))

    # A text item that already has a sub-rental booking is being sourced (it shows
    # in the Sub-rentals table), so drop it from the text-item flag — keep only the
    # free-text lines nothing has been arranged for yet. Then drop any job left with
    # neither a product shortage nor an un-sourced text item.
    subrent_names = defaultdict(set)
    for s in sub_rentals:
        subrent_names[s["id"]].add(s["item"])
    for sh in shortages:
        covered = subrent_names.get(sh["id"], set())
        sh["text_items"] = [t for t in sh.get("text_items", []) if t["name"] not in covered]
        sh["text_count"] = len(sh["text_items"])
    shortages = [sh for sh in shortages if sh["count"] or sh["text_count"]]

    # Suggested vendors: join each shorted/text item to the sub-rental history cache
    # (who we've sourced it from before). Catalog items match by product id, text
    # items by name. Done before the shared pass below pops the internal `pid`.
    vh = load_vendor_history()
    vh_pid, vh_name = vh.get("by_pid", {}), vh.get("by_name", {})
    def vendors_for(pid, name):
        v = vh_pid.get(str(pid)) if pid else None
        if not v and name:
            v = vh_name.get(name.strip().lower())
        return v or None
    for sh in shortages:
        for it in sh["items"]:
            v = vendors_for(it.get("pid"), it.get("name"))
            if v: it["vendors"] = v
        for it in sh.get("text_items", []):
            v = vendors_for(None, it.get("name"))
            if v: it["vendors"] = v

    # Mark SHARED shortages: the same product short on more than one opportunity in
    # the window. Those jobs compete for the SAME scarce stock, so their per-job
    # quantities are the same physical units — sourcing once can clear several jobs
    # (don't double-buy). Tag each such line and list the other jobs it clashes with.
    prod_jobs = defaultdict(list)   # pid -> [(oid, name)]
    for sh in shortages:
        for it in sh["items"]:
            prod_jobs[it["pid"]].append((sh["id"], sh["name"]))
    # Stable colour index per contested product so the SAME shared item lights up
    # the same colour across every job competing for it (sorted pid -> 0,1,2,...).
    shared_pids = sorted(pid for pid, jobs in prod_jobs.items()
                         if len({oid for oid, _ in jobs}) > 1)
    color_of = {pid: i for i, pid in enumerate(shared_pids)}
    for sh in shortages:
        for it in sh["items"]:
            others = sorted({nm for (oid, nm) in prod_jobs[it["pid"]] if oid != sh["id"]})
            if others:
                it["shared"] = True
                it["shared_color"] = color_of[it["pid"]]
                it["shared_with"] = others   # kept for the chip's hover tooltip only
            it.pop("pid", None)   # internal join key — keep it out of the payload

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

    # Supplier roster for the Sub-rentals panel: each upcoming vendor in the window
    # plus its address (pulled from the RMS member/organisation during supplier_name).
    # Names match the cleaned supplier string carried on each sub_rental line.
    name_addr = {info["name"]: info.get("address")
                 for info in _member.values() if info.get("name")}
    sup_names = sorted({s["supplier"] for s in sub_rentals if s.get("supplier")})
    suppliers = [{"name": nm, "address": name_addr.get(nm)} for nm in sup_names]

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "horizon_days": DAYS,
        "shift_hours": SHIFT,
        "min_hands": MIN_HANDS,
        "rms_base": f"https://{SUBDOMAIN}.current-rms.com",  # opp link = rms_base/opportunities/<id>
        "days": rows,
        "risk": risk,
        "missing_deprep": sorted(missing_deprep, key=lambda m: m["back_date"]),
        "sub_rentals": sub_rentals,
        "suppliers": suppliers,
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

    # LOGISTICS-PORT (phase 4 cutover): the in-app NATIVE TS compute (logisticsnativeforecast, gated by
    # LOGISTICS_NATIVE_COMPUTE) now OWNS logistics/forecast. To avoid a dual-writer race, this producer no
    # longer publishes the forecast itself — it publishes the vendor-history cache instead, so the native
    # compute can join supplier suggestions onto its shortages (it has no other source for them).
    # Rollback lever: set LOGISTICS_FORECAST_PUBLISH=1 to resume publishing the Python forecast (and turn
    # the native gate off) if the native output needs to be reverted.
    publish_to_app("vendorHistory", vh)
    if os.environ.get("LOGISTICS_FORECAST_PUBLISH") == "1":
        publish_to_app("forecast", payload)

if __name__ == "__main__":
    main()
