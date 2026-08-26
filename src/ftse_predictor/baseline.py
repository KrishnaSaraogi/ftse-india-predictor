"""
Shared constituent baseline loader - used by BOTH run_prediction.py (to
know what's currently a constituent when predicting) and
update_backtest.py (to diff against when new N-PORT data arrives).

Previously update_backtest.py had its own simpler, incomplete copy of
this logic that assumed a plain 'isin' column always existed - which
crashed the first time it actually ran against a real raw-FTSE-format
baseline file (confirmed live: "KeyError: 'isin'" on an otherwise
successful SEC EDGAR pull). Two implementations of the same logic
drifting out of sync is exactly how that happened. Now there is one.
"""

import re

import pandas as pd

from .universe import normalize_name


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
              f"_NAME_ALIASES in src/ftse_predictor/baseline.py and re-run.")

    isins = set(matched_isins)
    names = set(n for n in df["Constituent name"].map(normalize_name) if n)
    return isins, names, as_of
