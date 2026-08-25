"""
Builds the ranked candidate universe for a given data snapshot.

RANKS ON FULL MARKET CAP, NOT INVESTABLE MARKET CAP. This deviates
from the published Ground Rules (which specify IMC for the cumulative
coverage cut) and was a deliberate, evidence-based choice: IMC
cumulation was tested head-to-head against full mcap on identical data
and lost badly (73% vs 95%+ recall - see BACKTEST.md). The most likely
cause is that free-float data is too noisy to cumulate reliably (mean
absolute error ~6pp vs FTSE's own published investability weights,
concentrated in strategic-holder names). State this as a data-quality
substitution, not a methodology claim, if you present results externally.

The 88%/98% thresholds APPROXIMATE FTSE's real Asia-Pacific ex-Japan
ex-China regional cutoff using an India-only ranking. This is the
single largest remaining source of prediction error and cannot be
fixed without building the full regional universe.
"""

import re

import pandas as pd


def normalize_name(name: str) -> str:
    """Used to detect split-driven ISIN changes - a stock split changes
    the ISIN in India, so the pre- and post-split lines look like two
    different securities unless matched by name."""
    if not isinstance(name, str):
        return ""
    s = re.sub(r"[^A-Za-z0-9 ]", " ", name).upper()
    for w in (" LTD", " LIMITED", " CO", " CORP", " INC", " PLC", " NEW"):
        s = s.replace(w, " ")
    return re.sub(r"\s+", " ", s).strip()


def build_universe(panel: pd.DataFrame, snapshot_date: pd.Timestamp,
                   min_free_float: float) -> pd.DataFrame | None:
    """Ranked, screened universe at one panel snapshot. Does NOT know
    about constituent status - that is layered on by the caller using
    the appropriate PRIOR holdings snapshot, since membership must never
    be read at the review date itself (a stock added at date d is
    already in the holdings at d - reading membership there removes
    every genuine add from its own candidate pool)."""
    t = panel[(panel["review_date"] == pd.Timestamp(snapshot_date)) &
              panel["full_mcap"].notna() &
              (panel["free_float_pct"].fillna(0) > min_free_float)].copy()
    if t.empty:
        return None

    t["name_norm"] = t["name"].map(normalize_name)
    t = t.sort_values("full_mcap", ascending=False).reset_index(drop=True)
    t["cum_coverage"] = t["full_mcap"].cumsum() / t["full_mcap"].sum() * 100
    t["rank"] = t.index + 1
    return t


def tag_constituents(universe: pd.DataFrame, prior_isins: set[str],
                     prior_names_norm: set[str]) -> pd.DataFrame:
    """Adds is_constituent and is_split_duplicate columns, using the
    PRIOR holdings snapshot (before the review being predicted)."""
    u = universe.copy()
    u["is_constituent"] = u["isin_norm"].isin(prior_isins)
    u["is_split_duplicate"] = ((~u["is_constituent"]) &
                               u["name_norm"].isin(prior_names_norm))
    return u
