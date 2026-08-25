"""
FTSE's data cut-off dates, and listing-age lookups against them.

The cut-off lag was the single largest accuracy fix in this project's
development (see BACKTEST.md) - screening on review-date data instead
of the true cut-off silently produced lookahead and cost ~26 points of
hit rate. Every prediction MUST go through true_cutoff(), never use the
review date directly for eligibility checks.
"""

import json
import os

import pandas as pd

_CUTOFF_MONTH = {3: 1, 6: 4, 9: 7, 12: 10}


def true_cutoff(review_date: pd.Timestamp) -> pd.Timestamp | None:
    """FTSE screens on data as at the last business day of the month
    preceding the review by ~2 months (Jan/Apr/Jul/Oct for Mar/Jun/Sep/Dec
    reviews respectively). Returns None for a review month outside
    {3, 6, 9, 12}."""
    d = pd.Timestamp(review_date)
    cutoff_month = _CUTOFF_MONTH.get(d.month)
    if cutoff_month is None:
        return None
    end = pd.Timestamp(year=d.year, month=cutoff_month, day=1) + pd.offsets.MonthEnd(0)
    while end.weekday() >= 5:   # roll back off weekends
        end -= pd.Timedelta(days=1)
    return end


def review_effective_date(review_date: pd.Timestamp) -> pd.Timestamp:
    """The third Friday of the review month, effective the following
    Monday - informational only, not used in any screening logic."""
    d = pd.Timestamp(review_date)
    fridays = pd.date_range(d.replace(day=1), d + pd.offsets.MonthEnd(0), freq="W-FRI")
    return fridays[2]  # third Friday


class ListingDates:
    """Loads listing dates from the NSE cache once, exposes a fast
    lookup. Listing date is a fixed historical fact - no data
    freshness issue, unlike price or free float.

    Handles TWO cache shapes, since this project's manually-built cache
    (flat: {symbol: {...}}) and the automated fetcher's cache (nested
    under a source key: {"nse": {symbol: {...}}}) are structurally
    different. Reading the wrong shape here would silently return NaN
    for every symbol - no error, just wrong output - which disables
    BOTH the 3-month trading record rule and the entire fast-entry
    rule with no visible sign anything is wrong. Caught once during
    development; handled explicitly here so it cannot recur silently.
    """

    def __init__(self, nse_cache_path: str = "data/nse_tradeinfo_cache.json"):
        self._dates: dict[str, pd.Timestamp] = {}
        if os.path.exists(nse_cache_path):
            with open(nse_cache_path) as f:
                cache = json.load(f)
            # detect nested-under-source-key shape vs flat shape
            symbol_records = cache.get("nse", cache) if isinstance(cache, dict) else {}
            for sym, rec in symbol_records.items():
                ld = (rec or {}).get("listing_date")
                if ld:
                    try:
                        self._dates[sym] = pd.to_datetime(
                            ld, format="%d-%b-%Y %H:%M:%S")
                    except Exception:
                        pass
        else:
            print(f"WARNING: {nse_cache_path} not found. months_listed() will "
                  f"return NaN for every symbol, which disables the 3-month "
                  f"trading record rule AND the entire fast-entry rule.")

    def months_listed(self, symbol: str, at_date: pd.Timestamp) -> float:
        ld = self._dates.get(symbol)
        if ld is None:
            return float("nan")
        return (pd.Timestamp(at_date) - ld).days / 30.4
