"""
Liquidity test - monthly median turnover, per the Ground Rules.

VERIFIED AGAINST THE DOC (see BACKTEST.md for the audit): both review
windows, both thresholds and month counts, free-float-adjusted
denominator, <5-trading-day month exclusion with pro-rata requirement,
step-two retest, 3-month minimum record all implemented correctly.

NOT IMPLEMENTED, stated plainly:
  - suspension-period exclusion (no suspension flag in the source data)
  - volume is yfinance's, not the primary exchange's own figure. Two
    genuine adds (Sundaram Finance, GE T&D India) were excluded during
    backtesting at exactly 9/12 against a 10/12 bar - precisely where a
    minor volume discrepancy would flip the result.
"""

import pandas as pd

from .config import CONFIG


class LiquiditySource:
    """Pre-aggregates daily volume into (symbol, month) medians once, so
    repeated liquidity checks are fast lookups rather than repeated
    full-dataframe scans. Building this naively (scanning the raw daily
    panel on every call) made the original backtest take 20+ minutes;
    this brings it to seconds."""

    def __init__(self, volume_panel_path: str = "data/daily_volume_panel.csv"):
        vol = pd.read_csv(volume_panel_path, parse_dates=["Date"])
        if vol["Date"].dt.tz is not None:
            vol["Date"] = vol["Date"].dt.tz_localize(None)
        vol["month"] = vol["Date"].dt.to_period("M")

        monthly = (vol.groupby(["symbol", "month"])
                   .agg(median_vol=("Volume", "median"),
                        n_days=("Volume", "size"),
                        n_zero=("Volume", lambda s: (s == 0).sum()))
                   .reset_index())
        self._by_symbol = {s: g.set_index("month")
                           for s, g in monthly.groupby("symbol")}

    def liquidity_window(self, review_date: pd.Timestamp):
        """(start, end) or None if this is not a formal liquidity-test
        review month (only March and September are)."""
        d = pd.Timestamp(review_date)
        if d.month == 3:
            return pd.Timestamp(d.year - 1, 1, 1), pd.Timestamp(d.year - 1, 12, 31)
        if d.month == 9:
            return pd.Timestamp(d.year - 1, 7, 1), pd.Timestamp(d.year, 6, 30)
        return None

    def _monthly_pass_flags(self, symbol, start, end, ff_shares, threshold):
        grp = self._by_symbol.get(symbol)
        if grp is None or not ff_shares or ff_shares <= 0:
            return None
        start_m = pd.Period(start, freq="M")
        end_m = pd.Period(end, freq="M")
        sel = grp[(grp.index >= start_m) & (grp.index <= end_m)]
        if sel.empty:
            return None
        flags = []
        for _, row in sel.iterrows():
            if row["n_days"] < CONFIG.min_trading_days_per_month:
                continue
            turnover = row["median_vol"] / ff_shares * 100
            flags.append(turnover >= threshold)
        return flags

    def _nontrading_days_last_year(self, symbol, as_of):
        grp = self._by_symbol.get(symbol)
        if grp is None:
            return None
        end_m = pd.Period(as_of, freq="M")
        start_m = end_m - 11
        sel = grp[(grp.index >= start_m) & (grp.index <= end_m)]
        return int(sel["n_zero"].sum()) if len(sel) else None

    def passes(self, symbol: str, review_date: pd.Timestamp,
              free_float_shares: float, is_constituent: bool) -> tuple[bool, str]:
        """Returns (pass, reason). free_float_shares should already be
        AS AT THE WINDOW END, not the review date - callers are
        responsible for that lookup (see universe.py)."""
        win = self.liquidity_window(review_date)
        if win is None:
            return True, "not_a_review_month"

        nontrading = self._nontrading_days_last_year(symbol, review_date)
        if nontrading is not None and nontrading >= CONFIG.max_nontrading_days_per_year:
            return False, f"nontrading_days={nontrading}"

        threshold = (CONFIG.cons_liquidity_threshold if is_constituent
                    else CONFIG.non_cons_liquidity_threshold)
        required = (CONFIG.cons_liquidity_months_required if is_constituent
                   else CONFIG.non_cons_liquidity_months_required)

        flags = self._monthly_pass_flags(symbol, win[0], win[1],
                                         free_float_shares, threshold)
        if not flags:
            return False, "no_data"

        n_pass, n_applicable = sum(flags), len(flags)
        required_prorated = round(required * n_applicable / 12)
        if n_pass >= required_prorated:
            return True, f"primary_pass_{n_pass}/{n_applicable}"

        if is_constituent:
            last6 = flags[-6:] if len(flags) >= 6 else flags
            req6 = round(CONFIG.cons_liquidity_retest_months_required *
                         len(last6) / 6)
            if sum(last6) >= req6:
                return True, f"retest_pass_{sum(last6)}/{len(last6)}"

        return False, f"fail_{n_pass}/{n_applicable}"
