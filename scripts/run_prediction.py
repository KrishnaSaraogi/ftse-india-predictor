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
        if derived_review != review_date:
            print(f"NOTE: baseline reflects data as of {baseline_as_of.date()}. "
                  f"Overriding the requested review date "
                  f"({review_date.date()}) with the review this baseline can "
                  f"actually support: {derived_review.date()}.")
            review_date = derived_review
    else:
        print("WARNING: could not determine what quarter the baseline "
              "represents - using the requested/calendar-derived review "
              "date as-is. This may silently mispredict if the baseline "
              "is stale. Investigate before trusting this run's output.")

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


def _fuzzy_normalize(name):
    """Lighter than normalize_name() - strips only unambiguous suffixes,
    leaves the rest for the containment scorer to handle. Deliberately
    conservative, matching the approach already validated once in this
    project at 96% match rate."""
    import re
    if not isinstance(name, str):
        return frozenset()
    name = _NAME_ALIASES.get(name.strip().upper(), name.upper())
    words = re.findall(r"[A-Z0-9]+", name)
    stop = {"LTD", "LIMITED", "PLC", "INC", "CO", "COMPANY", "CORP",
           "CORPORATION", "THE", "AND", "OF", "INDIA", "INDIAN", "NEW"}
    core = [w for w in words if w not in stop]
    return frozenset(core or words)


# Known company renames that no name-similarity algorithm can bridge -
# confirmed during earlier work on this exact project (comparing FTSE's
# constituent file against the Nifty 500 universe). Extend this list if
# a future run reports an unmatched name that turns out to be a rename.
_NAME_ALIASES = {
    "ETERNAL": "ZOMATO",
    "LODHA DEVELOPERS": "MACROTECH DEVELOPERS",
    "GE VERNOVA T&D INDIA": "GE T&D INDIA",
}


def _fuzzy_match_name(target_name, candidate_names_normalized, threshold=0.5):
    """candidate_names_normalized: dict {original_name: frozenset_tokens}.
    Returns the best-matching original name, or None if nothing clears
    the threshold. Containment score (how much of the SHORTER token set
    is covered) handles cases like 'Titan' vs 'Titan Company', which
    plain Jaccard similarity scores too low."""
    from difflib import SequenceMatcher
    target_tokens = _fuzzy_normalize(target_name)
    if not target_tokens:
        return None, 0.0
    best_name, best_score = None, 0.0
    for cand_name, cand_tokens in candidate_names_normalized.items():
        if not cand_tokens:
            continue
        overlap = len(target_tokens & cand_tokens)
        containment = overlap / min(len(target_tokens), len(cand_tokens))
        jaccard = overlap / len(target_tokens | cand_tokens)
        score = max(containment * 0.9, jaccard)
        if score > best_score:
            best_name, best_score = cand_name, score
    return (best_name, best_score) if best_score >= threshold else (None, best_score)


def _next_quarter_end(date):
    """The FTSE review quarter-end immediately after the given date."""
    date = pd.Timestamp(date)
    quarter_ends = [3, 6, 9, 12]
    for m in quarter_ends:
        candidate = pd.Timestamp(year=date.year, month=m, day=1) + pd.offsets.MonthEnd(0)
        if candidate > date:
            return candidate
    return pd.Timestamp(year=date.year + 1, month=3, day=31)


def _nearest_quarter_end(date):
    """Rounds a raw file date (e.g. an FTSE export's header date) to the
    nearest quarter-end, since export dates land a few days around the
    actual review effective date, not exactly on the quarter-end."""
    date = pd.Timestamp(date)
    candidates = []
    for year_offset in (-1, 0, 1):
        for m in (3, 6, 9, 12):
            candidates.append(pd.Timestamp(year=date.year + year_offset, month=m, day=1)
                             + pd.offsets.MonthEnd(0))
    return min(candidates, key=lambda c: abs((c - date).days))


def _load_constituent_baseline(baseline_path, universe_df):
    """Handles TWO file formats for the constituent baseline:

    1. The simple format this project was designed around: columns
       literally named 'isin' and 'name', optionally 'as_of_date'
       (written automatically by update_backtest.py once real N-PORT
       data has been ingested at least once).

    2. FTSE's own raw constituent export (e.g. downloaded directly from
       research.ftserussell.com) - has 'Cons code', 'Constituent name',
       SEDOL, CUSIP, but NO ISIN (SEDOL-to-ISIN conversion is NOT valid
       for Indian securities - see comment below). ISIN is derived by
       FUZZY matching 'Constituent name' against eligible_universe.csv,
       and the file's own header date is extracted to determine what
       quarter this snapshot represents.

    Returns (isin_norm_set, name_norm_set, baseline_as_of_date_or_None).
    """
    raw = pd.read_csv(baseline_path, header=None, dtype=str,
                      engine="python", on_bad_lines="skip")

    first_row = [str(c).strip().lower() for c in raw.iloc[0].tolist()]
    if "isin" in first_row and "name" in first_row:
        df = pd.read_csv(baseline_path)
        isins = set(df["isin"].astype(str).str.strip().str.upper())
        names = set(n for n in df["name"].map(normalize_name) if n)
        as_of = None
        if "as_of_date" in df.columns and df["as_of_date"].notna().any():
            as_of = pd.Timestamp(df["as_of_date"].dropna().iloc[0])
        return isins, names, as_of

    header_row_idx = None
    for i in range(min(10, len(raw))):
        row_vals = [str(c).strip() for c in raw.iloc[i].tolist()]
        if "Cons code" in row_vals:
            header_row_idx = i
            break
    if header_row_idx is None:
        raise ValueError(
            f"{baseline_path} does not match either expected format "
            f"(simple isin/name columns, or FTSE's raw export with a "
            f"'Cons code' header row). Check the file manually.")

    # extract the file's own header date (e.g. "25/03/2026") from the
    # rows before the actual data header - this tells us what quarter
    # the snapshot represents, independent of wall-clock "today"
    as_of = None
    date_pattern = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
    for i in range(header_row_idx):
        for cell in raw.iloc[i].tolist():
            if not isinstance(cell, str):
                continue
            m = date_pattern.search(cell)
            if m:
                day, month, year = (int(x) for x in m.groups())
                try:
                    file_date = pd.Timestamp(year=year, month=month, day=day)
                    as_of = _nearest_quarter_end(file_date)
                except ValueError:
                    continue
                break
        if as_of is not None:
            break

    df = pd.read_csv(baseline_path, header=header_row_idx)
    df = df[df["Cons code"].notna()].copy()
    print(f"  detected FTSE raw export format: {len(df)} constituents"
         + (f", representing the {as_of.date()} review" if as_of is not None
            else " (could not determine which review this represents from "
                 "the file header)")
         + ", fuzzy-matching against eligible_universe.csv "
           "(ISIN cannot be derived from SEDOL for Indian securities)")

    universe_df = universe_df.copy()
    universe_names = {row["Company Name"]: _fuzzy_normalize(row["Company Name"])
                      for _, row in universe_df.iterrows()}
    isin_by_universe_name = dict(zip(universe_df["Company Name"],
                                     universe_df["ISIN Code"].astype(str)
                                     .str.strip().str.upper()))

    matched_isins, unmatched_names, low_confidence = [], [], []
    for _, row in df.iterrows():
        best_name, score = _fuzzy_match_name(row["Constituent name"], universe_names)
        if best_name is not None:
            matched_isins.append(isin_by_universe_name[best_name])
            if score < 0.8:
                low_confidence.append((row["Constituent name"], best_name, round(score, 2)))
        else:
            unmatched_names.append(row["Constituent name"])

    print(f"  matched {len(matched_isins)} of {len(df)} via fuzzy name match")
    if low_confidence:
        print(f"  {len(low_confidence)} matched with LOWER confidence (score < 0.8) - "
              f"worth spot-checking these:")
        for orig, matched, score in low_confidence[:10]:
            print(f"    '{orig}' -> '{matched}' (score {score})")
    if unmatched_names:
        print(f"  {len(unmatched_names)} names had NO match above threshold - "
              f"these will be excluded from the constituent baseline entirely, "
              f"meaning the model may wrongly predict them as new additions:")
        for nm in unmatched_names[:15]:
            print(f"    - {nm}")
        print(f"  If any of these are renamed companies, add them to "
              f"_NAME_ALIASES in this script and re-run.")

    isins = set(matched_isins)
    names = set(n for n in df["Constituent name"].map(normalize_name) if n)
    return isins, names, as_of


if __name__ == "__main__":
    main()
