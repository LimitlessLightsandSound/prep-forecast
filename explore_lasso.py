#!/usr/bin/env python3
"""
Lasso API explorer (READ-ONLY).

A throwaway-friendly probe for seeing what the Lasso API returns, so we can
decide what's worth pulling into the forecast. It only ever issues HTTPS GET
requests and prints the result — it writes nothing, anywhere, ever.

Usage:
  source .env
  python3 explore_lasso.py <path> [query=value ...]

Examples:
  python3 explore_lasso.py /                 # whatever the root returns
  python3 explore_lasso.py /users            # list endpoint guess
  python3 explore_lasso.py /events page=1     # with a query param

Env vars (set in .env):
  LASSO_API_TOKEN    your API token / key                          (required)
  LASSO_API_BASE     API base URL, e.g. https://api.lasso.io/v1    (required)

Auth scheme (confirmed for limitless.lasso.io — override only if it changes):
  LASSO_AUTH_HEADER  header name for the token   (default: LASSO-APIKEY)
  LASSO_AUTH_PREFIX  text before the token       (default: "" — none)

Interactive API reference (Swagger): https://limitless.lasso.io/api/v1/swagger/
"""

import os, sys, json
import urllib.request, urllib.parse, urllib.error

TOKEN  = os.environ.get("LASSO_API_TOKEN", "")
BASE   = os.environ.get("LASSO_API_BASE", "").rstrip("/")
HEADER = os.environ.get("LASSO_AUTH_HEADER", "LASSO-APIKEY")
PREFIX = os.environ.get("LASSO_AUTH_PREFIX", "")

# --- Read-only by construction (same posture as build_forecast.py) ---
# Only GET, only over TLS, and the token is only ever sent to the host named in
# LASSO_API_BASE. Anything else aborts instead of leaking the key or mutating.
ALLOWED_SCHEME = "https"


def get(path, params=None):
    if not TOKEN or not BASE:
        sys.exit("Missing LASSO_API_TOKEN or LASSO_API_BASE — set them in .env, then `source .env`.")
    url = BASE + ("/" + path.lstrip("/") if path else "")
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)

    parts = urllib.parse.urlsplit(url)
    if parts.scheme != ALLOWED_SCHEME:
        sys.exit(f"Refusing non-HTTPS request to {url!r} (token must only travel over TLS).")
    allowed_host = urllib.parse.urlsplit(BASE).netloc
    if parts.netloc != allowed_host:
        sys.exit(f"Refusing to send token to {parts.netloc!r}; only {allowed_host!r} is allowed.")

    req = urllib.request.Request(url, method="GET")  # GET only — never a write
    req.add_header(HEADER, f"{PREFIX}{TOKEN}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, raw
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return e.code, body
    except urllib.error.URLError as e:
        sys.exit(f"Network error reaching {url!r}: {e.reason}\n"
                 f"(Is LASSO_API_BASE correct? Currently {BASE!r}.)")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "/"
    params = dict(a.split("=", 1) for a in sys.argv[2:] if "=" in a)

    status, raw = get(path, params)
    print(f"GET {BASE}/{path.lstrip('/')}  ->  HTTP {status}\n")

    try:
        data = json.loads(raw)
    except ValueError:
        print(raw[:4000])
        return

    pretty = json.dumps(data, indent=2)
    print(pretty[:6000] + ("\n... (truncated)" if len(pretty) > 6000 else ""))

    # If it's a list of records, surface the field names of the first one —
    # that's the fastest way to see what data is available to pull.
    items = data if isinstance(data, list) else (
        next((v for v in data.values() if isinstance(v, list)), None)
        if isinstance(data, dict) else None)
    if items and isinstance(items[0], dict):
        print("\nFields on first record:")
        for k in items[0]:
            print(f"  - {k}")


if __name__ == "__main__":
    main()
