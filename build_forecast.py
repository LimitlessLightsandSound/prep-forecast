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

def main():
    today = dt.date.today()
    horizon = today + dt.timedelta(days=DAYS)

    # Active opportunities (default scope already excludes cancelled/dead).
    # Embed items so we don't make an N+1 call per opp.
    # NB: once any include[] is set, RMS only returns the associations you ask
    # for — so owner (the account manager) must be requested explicitly too.
    opps = get_all("/opportunities", "opportunities",
                   params={"include[]": ["opportunity_items", "owner"]})

    if DEBUG and opps:
        print("=== First opportunity raw keys ===")
        print(json.dumps({k: opps[0][k] for k in list(opps[0].keys())}, default=str, indent=2)[:4000])
        sys.exit(0)

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

    # --- Real shortage QUANTITIES ------------------------------------------
    # RMS flags a line `has_shortage` but never says HOW MANY are short, and the
    # public API exposes no availability figure. The line quantity is the full
    # booked amount, which massively over-states the shortage. We reconstruct the
    # shortfall the way RMS's Availability view does, over the job's window:
    #     supply = owned stock − quarantine − flagged-unavailable
    #     short  = min(qty this job booked, max(0, concurrent_demand − supply))
    # concurrent_demand = every active booking of that product whose window
    # overlaps this job's. QUARANTINE matters and is easy to miss: damaged/lost
    # gear sits in an open-ended quarantine record that does NOT appear in the
    # stock level's quantity_unavailable, yet RMS removes it from availability —
    # so ignoring it under-counts the shortage (e.g. 3 quarantined True1 cables
    # turn a "5 short" into the correct "8 short"). Capping at the job's own
    # quantity keeps the per-job number honest.
    prod_demand = defaultdict(list)   # product_id -> [(start, end, qty)]
    for opp in opps:
        for it in opp.get("opportunity_items", []):
            if it.get("item_type") != "Product":
                continue
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
            short_items = []
            for pid, g in by_prod.items():
                if g["start"] and g["end"]:
                    over = (concurrent_demand(pid, g["start"], g["end"])
                            - supply(pid, g["start"], g["end"]))
                    short = min(g["qty"], max(0.0, over))
                else:
                    short = g["qty"]
                # RMS flagged this product short; never hide that — floor at 1 unit.
                short = max(1, round(short))
                short_items.append({"name": g["name"], "qty": short,
                                    "group": group_name(pid), "pid": pid})
            # Organise by product group (POWER, CABLE, ...), then biggest shortage
            # first within a group, so the list reads like a pull sheet.
            short_items.sort(key=lambda x: (x["group"], -x["qty"], x["name"]))
            # Pickup = first prep day; return = de-prep day (the shop's own timing).
            prep_span = days_span(first_key(opp, PREP_START_KEYS), first_key(opp, OUT_KEYS))
            prep_days = (prep_span[:-1] or [prep_span[-1] - dt.timedelta(days=1)]) if prep_span else []
            deprep_days = days_span(first_key(opp, RETURN_START_KEYS), first_key(opp, RETURN_END_KEYS))
            shortages.append({
                "id": oid, "name": name, "number": opp.get("number"),
                "out_date": od.isoformat(),
                "pickup_date": prep_days[0].isoformat() if prep_days else None,
                "return_date": deprep_days[0].isoformat() if deprep_days else None,
                "count": len(short_items),
                "items": short_items,   # full list — no roll-off
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
