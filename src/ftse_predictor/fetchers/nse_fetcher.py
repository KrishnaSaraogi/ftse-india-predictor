"""
Refreshes free float, shares in issue, and listing dates from NSE.

This is the highest-risk source in the pipeline - unofficial session-
based scraping, no stability guarantee. Every value goes through
data_health checks before being accepted; anything that fails falls
back to the last known good value and gets logged, never silently
accepted or silently dropped.

Runs on a slower cadence than volume (see fetch_and_apply's
force_refresh flag) since shareholding filings are themselves
quarterly - refreshing this every single scheduled run would just
triple the NSE risk exposure for no real benefit, the underlying data
mostly hasn't changed.
"""

import time

import pandas as pd

from ..data_health import (LastKnownGoodCache, check_free_float,
                           check_mass_null_rate, check_shares)


def _fetch_raw(symbols: list[str], sleep_between: float = 0.25) -> dict[str, dict]:
    """Isolated so the network call is the only thing that needs
    changing if NSE's package/API shape changes again - the anomaly
    checking logic below never has to be touched for that."""
    try:
        from nse import NSE
    except ImportError:
        raise RuntimeError(
            "The 'nse' package is required (pip install nse). If NSE has "
            "changed their API in a way that breaks this package, that is "
            "exactly the kind of external change this project has hit "
            "before - check github.com/BennyThadikaran/NseIndiaApi for an "
            "update before assuming the data is simply unavailable.")

    raw = {}
    with NSE("") as client:
        for sym in symbols:
            rec = {}
            try:
                q = client.quote(symbol=sym)
                ti = q.get("tradeInfo") or {}
                si = q.get("secInfo") or {}
                rec = {
                    "issued_size": ti.get("issuedSize"),
                    "total_mcap": ti.get("totalMarketCap"),
                    "ffmc": ti.get("ffmc"),
                    "listing_date": si.get("listingDate"),
                }
            except Exception as e:
                rec = {"_error": f"{type(e).__name__}: {e}"}
            raw[sym] = rec
            time.sleep(sleep_between)
    return raw


def refresh_nse_data(symbols: list[str], cache_path: str, health_log,
                     as_of_date: str) -> dict[str, dict]:
    """Returns {symbol: {free_float_pct, shares, listing_date}}, using
    the cache as a fallback wherever a fetched value fails its sanity
    check. Updates and saves the cache with everything that passed."""
    cache = LastKnownGoodCache(cache_path)
    raw = _fetch_raw(symbols)

    n_null = sum(1 for r in raw.values() if r.get("_error") or
                r.get("total_mcap") is None)
    is_sane, reason = check_mass_null_rate(n_null, len(symbols))
    if not is_sane:
        health_log.record("nse_bulk", "ALL_SYMBOLS", reason)
        # systemic failure - do not attempt per-symbol acceptance, the
        # whole pull is untrustworthy. Fall back to cache for everyone.
        return {s: cache.get("nse", s) or {} for s in symbols}

    result = {}
    for sym in symbols:
        rec = raw.get(sym, {})
        prior = cache.get("nse", sym) or {}

        ff = None
        if rec.get("ffmc") and rec.get("total_mcap"):
            try:
                ff = rec["ffmc"] / rec["total_mcap"] * 100
            except (TypeError, ZeroDivisionError):
                ff = None

        ff_ok, ff_reason = check_free_float(ff, prior.get("free_float_pct"))
        shares_ok, shares_reason = check_shares(
            rec.get("issued_size"), prior.get("shares"))

        final = dict(prior)  # start from last known good
        if ff_ok:
            final["free_float_pct"] = ff
        else:
            health_log.record("nse_free_float", sym, ff_reason,
                              prior.get("_as_of_date"))
        if shares_ok:
            final["shares"] = rec.get("issued_size")
        else:
            health_log.record("nse_shares", sym, shares_reason,
                              prior.get("_as_of_date"))
        if rec.get("listing_date"):
            final["listing_date"] = rec["listing_date"]   # static fact, no anomaly check needed

        cache.set("nse", sym, final, as_of_date)
        result[sym] = final

    cache.save()
    return result
