#!/usr/bin/env python3
"""
Fully automated prediction run. Refreshes data, predicts, appends to
the permanent workbook. No manual steps.

USAGE
    python scripts/run_prediction.py --review-date 2026-09-30 [--refresh-nse]

--refresh-nse is only passed on the ~quarterly slow-refresh runs (see
predict.yml) - NSE free float/shares/listing barely change within a
quarter, so refreshing it on every single run would triple NSE risk
exposure for no real benefit. Volume ALWAYS refreshes (incremental,
cheap, needed for the liquidity test to stay current).
"""

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from ftse_predictor import CONFIG, ListingDates, LiquiditySource, PredictionEngine
from ftse_predictor.data_health import DataHealthLog
from ftse_predictor.fetchers import refresh_nse_data, refresh_volume_panel
from ftse_predictor.universe import normalize_name
from ftse_predictor.report import append_prediction, apply_formatting, write_health_warning

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
WORKBOOK_PATH = Path(__file__).resolve().parent.parent / "outputs" / "rebalancing_predictions.xlsx"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--review-date", required=True)
    ap.add_argument("--refresh-nse", action="store_true",
                    help="also refresh NSE free float/shares/listing data "
                         "(slow-cadence, ~quarterly)")
    args = ap.parse_args()

    review_date = pd.Timestamp(args.review_date)
    health_log = DataHealthLog()

    universe_path = DATA_DIR / "eligible_universe.csv"
    if not universe_path.exists():
        print(f"ERROR: {universe_path} not found.")
        print("This is a required one-time setup file - see data/README.md")
        print("for what needs to be uploaded before automation can run.")
        sys.exit(1)

    universe_df = pd.read_csv(universe_path)
    symbols = universe_df["Symbol"].astype(str).str.strip().tolist()

    # ---- volume: always refresh, incremental ----
    print("Refreshing volume panel (incremental)...")
    volume_panel_path = DATA_DIR / "daily_volume_panel.csv"
    vol_df = refresh_volume_panel(symbols, str(volume_panel_path), health_log)
    vol_df.to_csv(volume_panel_path, index=False)
    print(f"  {len(vol_df):,} total rows in volume panel")

    # ---- NSE: only on slow-cadence runs ----
    nse_cache_path = DATA_DIR / "nse_cache.json"
    must_refresh = args.refresh_nse or not nse_cache_path.exists()
    if must_refresh:
        print("Refreshing NSE data (free float, shares, listing dates)...")
        refresh_nse_data(symbols, str(nse_cache_path), health_log,
                         as_of_date=str(review_date.date()))
    else:
        print("Skipping NSE refresh (not a slow-cadence run) - using cache")

    if health_log.has_issues:
        print(f"\n{len(health_log.events)} data health issues this run:")
        for line in health_log.summary_lines():
            print(f"  - {line}")

    # ---- build panel from cache + latest prices for prediction ----
    panel = _build_panel_from_cache(str(nse_cache_path), vol_df, review_date)

    listing_dates = ListingDates(str(nse_cache_path))
    liquidity = LiquiditySource(str(volume_panel_path))
    engine = PredictionEngine(panel, listing_dates, liquidity)

    baseline_path = DATA_DIR / "constituents_baseline.csv"
    if not baseline_path.exists():
        print(f"ERROR: {baseline_path} not found - cannot determine current "
              f"constituents. See README.")
        sys.exit(1)
    baseline = pd.read_csv(baseline_path)
    baseline["isin_norm"] = baseline["isin"].astype(str).str.strip().str.upper()
    baseline["name_norm"] = baseline["name"].map(normalize_name)

    result = engine.predict(review_date, panel["review_date"].max(),
                            set(baseline["isin_norm"]),
                            set(n for n in baseline["name_norm"] if n))
    if result is None:
        print("ERROR: could not build a universe for this review date.")
        sys.exit(1)

    print(f"\nQuarter type: {result.quarter_type}")
    print(f"Predicted adds: {len(result.adds)}, removes: {len(result.removes)}")

    append_prediction(str(WORKBOOK_PATH), result)
    if health_log.has_issues:
        write_health_warning(str(WORKBOOK_PATH), health_log)
    apply_formatting(str(WORKBOOK_PATH))
    print(f"\nAppended to {WORKBOOK_PATH}")

    # log for update_backtest.py to score later
    pending_path = Path(__file__).resolve().parent.parent / "outputs" / "pending_predictions.csv"
    isin_by_symbol = dict(zip(panel["symbol"], panel["isin_norm"])) if "isin_norm" in panel.columns else {}
    pending_row = pd.DataFrame([{
        "review_date": review_date.date(), "quarter_type": result.quarter_type,
        "predicted_add_symbols": ";".join(result.adds["symbol"]) if len(result.adds) else "",
        "predicted_remove_symbols": ";".join(result.removes["symbol"]) if len(result.removes) else "",
    }])
    if pending_path.exists():
        existing = pd.read_csv(pending_path)
        existing = existing[existing["review_date"] != str(review_date.date())]
        pending_row = pd.concat([existing, pending_row], ignore_index=True)
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    pending_row.to_csv(pending_path, index=False)


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
