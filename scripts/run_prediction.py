#!/usr/bin/env python3
"""
Fully automated prediction run. Refreshes data, predicts, appends to
the permanent workbook. No manual steps.

USAGE
    python scripts/run_prediction.py [--review-date 2026-09-30] [--refresh-nse]

--review-date is OPTIONAL and rarely needed - the target review is
normally DERIVED from the constituent baseline's own effective date
(baseline quarter + 1). Passing it explicitly overrides that, but will
be immediately overridden right back if it doesn't match what the
baseline can actually support - see the override logic below. This
exists mainly for manual testing, not routine use.

--refresh-nse forces an NSE refresh regardless of cache age. Normally
this decides itself: NSE free float/shares/listing barely change
within a quarter, so the cache is only refreshed if it's more than
~75 days old, based on ANY scheduled run noticing staleness - not tied
to a specific calendar day, since the schedule now runs twice a month
year-round rather than only near cut-off dates.
"""

import argparse
import os
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from ftse_predictor import CONFIG, ListingDates, LiquiditySource, PredictionEngine
from ftse_predictor.data_health import DataHealthLog
from ftse_predictor.fetchers import refresh_nse_data, refresh_volume_panel
from ftse_predictor.universe import normalize_name
from ftse_predictor.report import (append_prediction, apply_formatting,
                                   write_health_warning, annotate_summary_data_warning)
from ftse_predictor.baseline import _load_constituent_baseline, _next_quarter_end

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
WORKBOOK_PATH = Path(__file__).resolve().parent.parent / "outputs" / "rebalancing_predictions.xlsx"
NSE_REFRESH_MAX_AGE_DAYS = 75   # ~ a quarter - NSE shareholding filings
                                # are themselves quarterly, so refreshing
                                # more often than this buys nothing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--review-date", default=None,
                    help="optional override - normally derived from the "
                         "baseline's own date instead")
    ap.add_argument("--refresh-nse", action="store_true",
                    help="force an NSE refresh regardless of cache age")
    args = ap.parse_args()

    review_date = pd.Timestamp(args.review_date) if args.review_date else None
    health_log = DataHealthLog()

    universe_path = DATA_DIR / "eligible_universe.csv"
    if not universe_path.exists():
        print(f"ERROR: {universe_path} not found.")
        print("This is a required one-time setup file - see data/README.md")
        print("for what needs to be uploaded before automation can run.")
        sys.exit(1)

    universe_df = pd.read_csv(universe_path)
    symbols = universe_df["Symbol"].astype(str).str.strip().tolist()

    baseline_path = DATA_DIR / "constituents_baseline.csv"
    if not baseline_path.exists():
        print(f"ERROR: {baseline_path} not found - cannot determine current "
              f"constituents. See README.")
        sys.exit(1)
    baseline_isins, baseline_names, baseline_as_of = _load_constituent_baseline(
        str(baseline_path), universe_df)

    # THE FIX: predict the review immediately AFTER whatever quarter the
    # baseline actually represents - never trust the workflow's
    # calendar-guessed date, which has no idea what quarter the loaded
    # data reflects. Using today's date instead of the baseline's date
    # caused real June 2026 additions to be silently re-predicted as
    # "September" candidates, since March data was being used as if it
    # were current going into September.
    if baseline_as_of is not None:
        derived_review = _next_quarter_end(baseline_as_of)
        if review_date is None:
            print(f"Baseline reflects data as of {baseline_as_of.date()} - "
                  f"predicting the next review: {derived_review.date()}.")
            review_date = derived_review
        elif derived_review != review_date:
            print(f"NOTE: baseline reflects data as of {baseline_as_of.date()}. "
                  f"Overriding the requested review date "
                  f"({review_date.date()}) with the review this baseline can "
                  f"actually support: {derived_review.date()}.")
            review_date = derived_review
    elif review_date is None:
        print("ERROR: could not determine what quarter the baseline "
              "represents, and no --review-date was given to fall back on. "
              "Cannot proceed safely.")
        sys.exit(1)
    else:
        print("WARNING: could not determine what quarter the baseline "
              "represents - using the explicitly-provided review date "
              "as-is. This may mispredict if the baseline is stale.")

    # ---- volume: always refresh, incremental ----
    print("Refreshing volume panel (incremental)...")
    volume_panel_path = DATA_DIR / "daily_volume_panel.csv"
    vol_df = refresh_volume_panel(symbols, str(volume_panel_path), health_log)
    vol_df.to_csv(volume_panel_path, index=False)
    print(f"  {len(vol_df):,} total rows in volume panel")

    # ---- NSE: refresh only if the cache is genuinely stale, not tied to
    # any particular calendar day (the schedule now runs twice a month
    # year-round, not just near cut-off dates) ----
    nse_cache_path = DATA_DIR / "nse_cache.json"
    cache_age_days = None
    if nse_cache_path.exists():
        cache_age_days = (time.time() - os.path.getmtime(nse_cache_path)) / 86400
    must_refresh = (args.refresh_nse or cache_age_days is None or
                    cache_age_days > NSE_REFRESH_MAX_AGE_DAYS)
    if must_refresh:
        reason = ("forced" if args.refresh_nse else
                  "no cache yet" if cache_age_days is None else
                  f"cache is {cache_age_days:.0f} days old")
        print(f"Refreshing NSE data (free float, shares, listing dates) - {reason}...")
        refresh_nse_data(symbols, str(nse_cache_path), health_log,
                         as_of_date=str(review_date.date()))
    else:
        print(f"Skipping NSE refresh - cache is only {cache_age_days:.0f} days "
              f"old (refreshes past {NSE_REFRESH_MAX_AGE_DAYS} days)")

    if health_log.has_issues:
        print(f"\n{len(health_log.events)} data health issues this run:")
        for line in health_log.summary_lines():
            print(f"  - {line}")

    # ---- build panel from cache + latest prices for prediction ----
    panel = _build_panel_from_cache(str(nse_cache_path), vol_df, review_date)

    listing_dates = ListingDates(str(nse_cache_path))
    liquidity = LiquiditySource(str(volume_panel_path))
    engine = PredictionEngine(panel, listing_dates, liquidity)

    result = engine.predict(review_date, panel["review_date"].max(),
                            baseline_isins, baseline_names)
    if result is None:
        print("ERROR: could not build a universe for this review date.")
        sys.exit(1)

    print(f"\nQuarter type: {result.quarter_type}")
    print(f"Predicted adds: {len(result.adds)}, removes: {len(result.removes)}")

    append_prediction(str(WORKBOOK_PATH), result)
    if health_log.has_issues:
        write_health_warning(str(WORKBOOK_PATH), health_log)
        annotate_summary_data_warning(str(WORKBOOK_PATH), review_date)
    apply_formatting(str(WORKBOOK_PATH))
    print(f"\nAppended to {WORKBOOK_PATH}")


def _build_panel_from_cache(nse_cache_path, vol_df, review_date):
    """Builds the single-snapshot panel the prediction engine expects,
    from the NSE cache (free float, shares) and the latest available
    price in the volume panel."""
    import json
    with open(nse_cache_path) as f:
        cache = json.load(f).get("nse", {})

    latest_prices = (vol_df.sort_values("Date").groupby("symbol")
                     .last()[["Close"]].rename(columns={"Close": "price"}))

    rows = []
    for sym, rec in cache.items():
        if sym not in latest_prices.index:
            continue
        price = latest_prices.loc[sym, "price"]
        shares = rec.get("shares")
        ff = rec.get("free_float_pct")
        if not shares or ff is None:
            continue
        rows.append({
            "review_date": review_date, "symbol": sym, "name": sym,
            "isin_norm": sym,  # placeholder - real ISIN comes from universe file
            "price": price, "shares": shares, "free_float_pct": ff,
            "full_mcap": price * shares,
        })
    panel = pd.DataFrame(rows)

    # attach real name/isin from the universe file for display + matching
    uni = pd.read_csv(Path(__file__).resolve().parent.parent / "data" / "eligible_universe.csv")
    uni["isin_norm"] = uni["ISIN Code"].astype(str).str.strip().str.upper()
    name_map = dict(zip(uni["Symbol"], uni["Company Name"]))
    isin_map = dict(zip(uni["Symbol"], uni["isin_norm"]))
    panel["name"] = panel["symbol"].map(name_map).fillna(panel["symbol"])
    panel["isin_norm"] = panel["symbol"].map(isin_map).fillna(panel["symbol"])
    return panel


if __name__ == "__main__":
    main()
