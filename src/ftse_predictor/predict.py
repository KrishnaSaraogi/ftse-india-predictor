"""
The prediction engine. Two populations, two different rules - confirmed
by a clean, near-perfect separation in the data (11/11 historical
Jun/Dec adds were listed under 12 months; median Mar/Sep listing age
was 193 months). See BACKTEST.md for the full evidence trail.

    MARCH / SEPTEMBER (general semi-annual review)
        coverage <= entry_coverage (India-only proxy for the regional cut)
        liquidity test
        3-month minimum trading record
        not already a constituent, not a split-duplicate

    JUNE / DECEMBER (fast entry only)
        listed < fast_entry_max_months
        market cap >= fast_entry_min_mcap_cr
        NOT screened on coverage, liquidity, or trading record - the
        Ground Rules exempt fast entrants from the general review tests

    REMOVES (both quarter types, same rule)
        coverage > exit_coverage, OR fails the constituent liquidity
        exit test (liquidity only formally tested Mar/Sep)
        REMOVES ARE NOT STATISTICALLY VALIDATED - only ~4 genuine
        events exist in the entire historical ground truth this model
        was built against. Treat any removes output as a low-confidence
        directional watchlist, never as a call.
"""

from dataclasses import dataclass, field

import pandas as pd

from .config import CONFIG, Config
from .cutoffs import ListingDates, true_cutoff
from .liquidity import LiquiditySource
from .universe import build_universe, tag_constituents


@dataclass
class PredictionResult:
    review_date: pd.Timestamp
    quarter_type: str   # "general_review" or "fast_entry"
    adds: pd.DataFrame
    removes: pd.DataFrame
    universe_size: int
    n_constituents: int


class PredictionEngine:
    def __init__(self, panel: pd.DataFrame, listing_dates: ListingDates,
                liquidity: LiquiditySource, config: Config = CONFIG):
        self.panel = panel
        self.listing_dates = listing_dates
        self.liquidity = liquidity
        self.config = config

    def predict(self, review_date: pd.Timestamp, snapshot_date: pd.Timestamp,
               prior_isins: set[str], prior_names_norm: set[str]) -> PredictionResult | None:
        """
        review_date    the FTSE review being predicted (e.g. 2026-09-30)
        snapshot_date  which panel snapshot to rank on (usually the most
                       recent available - see README on the true-cutoff
                       pricing experiment for why this proxy is used
                       deliberately rather than chased further)
        prior_isins / prior_names_norm
                       the constituent baseline BEFORE this review -
                       from your most recent real N-PORT holdings, never
                       from data as-of the review date itself
        """
        d = pd.Timestamp(review_date)
        is_review_month = d.month in (3, 9)
        cfg = self.config

        u = build_universe(self.panel, snapshot_date, cfg.min_free_float)
        if u is None:
            return None
        u = tag_constituents(u, prior_isins, prior_names_norm)

        # precompute once - NOT inside any per-row apply(). Re-filtering
        # the dataframe by symbol inside a loop was the exact mistake
        # that made the original liquidity check take 20+ minutes before
        # being fixed with this same pre-aggregation approach.
        u["ff_shares"] = u["shares"] * u["free_float_pct"] / 100

        tc = true_cutoff(d) or pd.Timestamp(snapshot_date)
        u["months_listed"] = u["symbol"].apply(
            lambda s: self.listing_dates.months_listed(s, tc))

        if is_review_month:
            adds = self._general_review_adds(u, tc)
        else:
            adds = self._fast_entry_adds(u, tc)

        removes = self._removes(u, d)

        return PredictionResult(
            review_date=d,
            quarter_type="general_review" if is_review_month else "fast_entry",
            adds=adds, removes=removes,
            universe_size=len(u), n_constituents=int(u["is_constituent"].sum()))

    def _general_review_adds(self, u: pd.DataFrame, true_cutoff_date) -> pd.DataFrame:
        cfg = self.config
        ok = ((~u["is_constituent"]) & (~u["is_split_duplicate"]) &
              (u["cum_coverage"] <= cfg.entry_coverage))
        idx = ok[ok].index
        if len(idx):
            sub = u.loc[idx]
            liq = [self.liquidity.passes(sym, true_cutoff_date, ffs, False)[0]
                  for sym, ffs in zip(sub["symbol"], sub["ff_shares"])]
            ok.loc[idx] = ok.loc[idx] & pd.Series(liq, index=idx).values
            ml_ok = ((u.loc[idx, "months_listed"] >= cfg.min_trading_record_months) |
                     u.loc[idx, "months_listed"].isna())
            ok.loc[idx] = ok.loc[idx] & ml_ok.values
        return u[ok.fillna(False)].sort_values("rank").reset_index(drop=True)

    def _fast_entry_adds(self, u: pd.DataFrame, true_cutoff_date) -> pd.DataFrame:
        cfg = self.config
        ok = ((~u["is_constituent"]) & (~u["is_split_duplicate"]) &
              (u["months_listed"] < cfg.fast_entry_max_months) &
              (u["full_mcap"] / 1e7 >= cfg.fast_entry_min_mcap_cr))
        return (u[ok.fillna(False)]
                .sort_values("full_mcap", ascending=False).reset_index(drop=True))

    def _removes(self, u: pd.DataFrame, review_date: pd.Timestamp) -> pd.DataFrame:
        cfg = self.config
        con = u[u["is_constituent"]].copy()
        fail_coverage = con["cum_coverage"] > cfg.exit_coverage

        fail_liquidity = pd.Series(False, index=con.index)
        if self.liquidity.liquidity_window(review_date) is not None and len(con):
            liq = [self.liquidity.passes(sym, review_date, ffs, True)[0]
                  for sym, ffs in zip(con["symbol"], con["ff_shares"])]
            fail_liquidity = ~pd.Series(liq, index=con.index).values

        zone = con[fail_coverage.values | fail_liquidity]
        return zone.sort_values("cum_coverage", ascending=False).reset_index(drop=True)
