#!/usr/bin/env python3
"""
Vendor-history cache builder for the Prep Forecast dashboard.

Crawls ~18 months of sub-rentals from Current RMS and records, per item, which
suppliers we've sourced it from before (ranked by how often, with the last date).
Writes docs/vendor_history.json — ENCRYPTED when FORECAST_PASSPHRASE is set, so it
is safe to commit to the public repo.

WHY this is a separate script: the crawl is expensive (a few hundred nested API
calls, several minutes). The 3-hour forecast pipeline only READS this file and
joins it to the current shortages, so it never pays the crawl cost. Re-run this
occasionally (e.g. weekly) to refresh the suggestions — vendor relationships move
slowly, so a stale-by-a-week cache is fine.

Run:  source .env && python3 build_vendor_history.py
Env:  same CURRENT_RMS_TOKEN / CURRENT_RMS_SUBDOMAIN / FORECAST_PASSPHRASE as the
      forecast pipeline (reused via `import build_forecast`).
"""
import os, json, datetime as dt
from collections import defaultdict
import build_forecast as bf

HISTORY_DAYS = 548   # 18 months, locked
TOP_VENDORS  = 3     # how many suggestions to keep per item
OUT = os.path.join(os.path.dirname(__file__), "docs", "vendor_history.json")


def clean_supplier(nm):
    """Member names come as 'Company | Contact' or 'SUPPLIER | Company' — reduce to
    the company so a suggestion chip reads cleanly (e.g. 'AudioTek', 'PRG')."""
    nm = (nm or "").replace("(SUPPLIER)", "").strip()
    parts = [p.strip() for p in nm.split("|")]
    parts = [p for p in parts if p and p.upper() != "SUPPLIER"]
    return parts[0] if parts else (nm or None)


def main():
    since = (dt.date.today() - dt.timedelta(days=HISTORY_DAYS)).isoformat()
    # Only opportunities that actually carry a sub-rent line, across ALL states
    # (filtermode=all reaches completed/archived history, not just the active book).
    opps = bf.get_all("/opportunities", "opportunities",
                      params={"filtermode": "all", "q[starts_at_gteq]": since,
                              "q[opportunity_items_sub_rent_eq]": "true",
                              "q[s]": "starts_at desc"})
    print(f"Crawling {len(opps)} opportunities with sub-rent lines since {since} …")

    _names = {}
    def supplier(sid):
        if not sid:
            return None
        if sid not in _names:
            try:
                nm = (bf.req(f"/members/{sid}").get("member", {}) or {}).get("name")
            except SystemExit:
                nm = None
            _names[sid] = clean_supplier(nm)
        return _names[sid]

    # key -> supplier -> {n: times used, last: most recent rental start date}
    by_pid  = defaultdict(lambda: defaultdict(lambda: {"n": 0, "last": ""}))
    by_name = defaultdict(lambda: defaultdict(lambda: {"n": 0, "last": ""}))
    for i, o in enumerate(opps):
        started = (o.get("starts_at") or "")[:10]
        try:
            nested = bf.get_all(f"/opportunities/{o['id']}/opportunity_items",
                                "opportunity_items", page_size=100)
        except SystemExit:
            continue
        for it in nested:
            for a in (it.get("item_assets") or []):
                if not a.get("sub_rent"):
                    continue
                sup = supplier(a.get("supplier_id"))
                if not sup:
                    continue   # unallocated sub-rent — no vendor to suggest
                pid = it.get("item_id")
                nm  = (it.get("name") or "").strip().lower()
                if pid and pid != 1:          # 1 = the placeholder id text items carry
                    r = by_pid[str(pid)][sup]; r["n"] += 1
                    if started > r["last"]: r["last"] = started
                if nm:
                    r = by_name[nm][sup]; r["n"] += 1
                    if started > r["last"]: r["last"] = started
        if (i + 1) % 50 == 0:
            print(f"  … {i+1}/{len(opps)}")

    def rank(m):
        out = {}
        for k, vendors in m.items():
            ranked = sorted(vendors.items(),
                            key=lambda kv: (kv[1]["n"], kv[1]["last"]), reverse=True)
            out[k] = [{"name": v, "n": d["n"], "last": d["last"]}
                      for v, d in ranked[:TOP_VENDORS]]
        return out

    payload = {
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "days": HISTORY_DAYS,
        "by_pid": rank(by_pid),
        "by_name": rank(by_name),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    raw = json.dumps(payload, indent=2).encode("utf-8")
    if bf.PASSPHRASE:
        with open(OUT, "w") as f:
            json.dump(bf.encrypt_envelope(raw, bf.PASSPHRASE), f, indent=2)
        print(f"Wrote ENCRYPTED {OUT}: "
              f"{len(payload['by_pid'])} products, {len(payload['by_name'])} item-names")
    else:
        with open(OUT, "wb") as f:
            f.write(raw)
        print(f"Wrote PLAINTEXT {OUT} (no FORECAST_PASSPHRASE) — do NOT publish")


if __name__ == "__main__":
    main()
