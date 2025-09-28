# main.py
# Simple BIN lookup API for Vercel (@vercel/python)
#
# Usage:
#   GET /?bin=457173
#   GET /?bin=457173,524353
#   GET /?bin=457173&bin=524353
#
# Notes:
# - Only accepts 6-8 digit BINs (no full PANs).
# - Primary source: https://lookup.binlist.net/{bin}
# - Optional fallback: ApiNinjas (set API_NINJAS_KEY environment var in Vercel)
# - This uses an in-memory cache (resets on cold start). For production, use Redis/Upstash.

from flask import Flask, request, jsonify
import requests
import re
import os
import time
from threading import Lock

app = Flask(__name__)

BIN_RE = re.compile(r"^\d{6,8}$")
CACHE_TTL = 24 * 60 * 60  # 24 hours
_cache = {}
_cache_lock = Lock()

API_NINJAS_KEY = os.environ.get("LieuYDhlNT/270kC+AdPLA==agp4Q2D3T2IckmlO")  # optional fallback

def get_cached(bin_str):
    with _cache_lock:
        entry = _cache.get(bin_str)
        if not entry:
            return None
        ts, data = entry
        if time.time() - ts > CACHE_TTL:
            del _cache[bin_str]
            return None
        return data

def set_cache(bin_str, data):
    with _cache_lock:
        _cache[bin_str] = (time.time(), data)

def normalize_binlist_response(raw):
    # raw is the JSON returned by binlist.net (or normalized fallback)
    if not raw:
        return None
    out = {}
    # fields vary across providers — try to pick common ones
    out["scheme"] = raw.get("scheme") or raw.get("brand") or None
    out["brand"] = raw.get("brand") or raw.get("scheme") or None
    out["type"] = raw.get("type") or None
    out["prepaid"] = raw.get("prepaid") if "prepaid" in raw else None
    bank = raw.get("bank") or {}
    # some fallback providers might return bank as string
    if isinstance(bank, str):
        bank = {"name": bank}
    out["bank"] = {"name": bank.get("name") or None, "url": bank.get("url") or None}
    country = raw.get("country") or {}
    out["country"] = {"name": country.get("name") or None, "alpha2": country.get("alpha2") or country.get("alpha") or None}
    # other useful hints if present
    out["number"] = {}
    if "number" in raw and isinstance(raw.get("number"), dict):
        out["number"]["length"] = raw["number"].get("length")
        out["number"]["luhn"] = raw["number"].get("luhn")
    return out

def lookup_bin_binlist(bin_str):
    url = f"https://lookup.binlist.net/{bin_str}"
    # binlist recommends sending Accept-Version header
    headers = {"Accept-Version": "3"}
    r = requests.get(url, headers=headers, timeout=8)
    if r.status_code == 200:
        return r.json()
    if r.status_code == 404:
        return None
    if r.status_code == 429:
        raise RuntimeError("binlist_rate_limited")
    # for other statuses, raise to trigger fallback behavior
    r.raise_for_status()

def lookup_bin_apininjas(bin_str, api_key):
    url = f"https://api.api-ninjas.com/v1/bin?bin={bin_str}"
    headers = {"X-Api-Key": api_key}
    r = requests.get(url, headers=headers, timeout=8)
    if r.status_code == 200:
        # ApiNinjas returns an array or object depending on plan; handle both
        data = r.json()
        # If it's a list, take first element
        if isinstance(data, list) and len(data) > 0:
            data = data[0]
        # Normalize ApiNinjas shape to similar keys as binlist
        normalized = {
            "scheme": data.get("scheme") or data.get("brand") or data.get("type"),
            "brand": data.get("brand") or data.get("scheme"),
            "type": data.get("type"),
            "bank": {"name": data.get("bank") or data.get("issuer") or data.get("brand")},
            "country": {"name": data.get("country"), "alpha2": data.get("country_code")}
        }
        return normalized
    if r.status_code == 404:
        return None
    if r.status_code == 429:
        raise RuntimeError("api_ninjas_rate_limited")
    r.raise_for_status()

@app.route("/", methods=["GET"])
def root():
    # Accept either single 'bin' with comma-separated values OR repeated 'bin' params.
    bins_param = request.args.getlist("bin")
    if not bins_param:
        single = request.args.get("bin")
        if single:
            bins_param = [single]
    # if still none, return usage/help
    if not bins_param:
        return jsonify({
            "error": "missing_parameter",
            "message": "Provide 'bin' parameter. Example: /?bin=457173 or /?bin=457173,524353 or /?bin=457173&bin=524353"
        }), 400

    # flatten comma-separated values
    bins = []
    for b in bins_param:
        for part in str(b).split(","):
            val = part.strip()
            if val:
                bins.append(val)
    # Deduplicate while preserving order
    seen = set()
    bins_filtered = []
    for b in bins:
        if b not in seen:
            seen.add(b)
            bins_filtered.append(b)
    bins = bins_filtered

    if len(bins) > 100:
        return jsonify({"error": "too_many_bins", "message": "Max 100 BINs per request"}), 400

    results = {}
    for bin_str in bins:
        if not BIN_RE.match(bin_str):
            results[bin_str] = {"error": "invalid_bin", "message": "BIN must be 6 to 8 digits"}
            continue

        # Check cache
        cached = get_cached(bin_str)
        if cached is not None:
            results[bin_str] = {"data": cached, "source": "cache"}
            continue

        # Try binlist.net first
        try:
            raw = lookup_bin_binlist(bin_str)
            if raw:
                normalized = normalize_binlist_response(raw)
                set_cache(bin_str, normalized)
                results[bin_str] = {"data": normalized, "source": "binlist"}
                continue
            # if not found (404) we may try fallback below
        except RuntimeError as ex:
            # handle rate limit indications by trying fallback if available
            err = str(ex)
            # fallthrough to fallback
        except Exception as e:
            # Unexpected error from binlist; track message and try fallback
            err = str(e)

        # Fallback: ApiNinjas if configured
        if API_NINJAS_KEY:
            try:
                raw2 = lookup_bin_apininjas(bin_str, API_NINJAS_KEY)
                if raw2:
                    normalized2 = normalize_binlist_response(raw2)
                    set_cache(bin_str, normalized2)
                    results[bin_str] = {"data": normalized2, "source": "api_ninjas"}
                    continue
                else:
                    results[bin_str] = {"error": "not_found", "message": "BIN not found in upstream APIs"}
                    continue
            except RuntimeError as ex:
                results[bin_str] = {"error": "upstream_rate_limited", "message": str(ex)}
                continue
            except Exception as e:
                results[bin_str] = {"error": "upstream_error", "message": str(e)}
                continue
        else:
            # No fallback configured; return not found or upstream error
            # If raw was None (404) — not found
            try:
                # If raw was None, earlier branch would have continued; so here we treat as not_found
                results[bin_str] = {"error": "not_found_or_upstream_error", "message": "Not found or upstream error. Set API_NINJAS_KEY to enable fallback."}
            except Exception:
                results[bin_str] = {"error": "unknown_error"}
            continue

    return jsonify({"results": results}), 200

# If running locally for testing
if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
