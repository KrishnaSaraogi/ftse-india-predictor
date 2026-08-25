"""
All model thresholds in one place. Every number here was validated
against a real backtest before being adopted - see the project's
backtest history (BACKTEST.md) for the evidence behind each one.

If you change a number here, re-run the backtest before trusting the
next prediction - a threshold that looks reasonable can silently hurt
accuracy (this happened twice in development: the dual-threshold Jun/Dec
attempt and the true-cutoff pricing experiment both looked like
improvements and were not, once tested properly).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    # --- General review (March / September) ---
    entry_coverage: float = 88.0
    exit_coverage: float = 98.0
    min_free_float: float = 5.0          # strictly greater than, per FTSE rules
    min_trading_record_months: float = 3.0

    # --- Fast entry (June / December) ---
    # CONFIRMED MECHANISM: 11/11 historical Jun/Dec adds were listed
    # under 12 months (median 5.5mo). Clean separation from Mar/Sep
    # adds (median 193mo). The coverage percentile is the WRONG
    # instrument for this population - do not apply it here.
    fast_entry_max_months: float = 12.0
    fast_entry_min_mcap_cr: float = 30000.0   # PROVISIONAL - see BACKTEST.md,
                                               # calibrated on only 11 events

    # --- Liquidity test (general review only) ---
    non_cons_liquidity_threshold: float = 0.050     # % of free-float shares
    non_cons_liquidity_months_required: int = 10    # of 12
    cons_liquidity_threshold: float = 0.040
    cons_liquidity_months_required: int = 8         # of 12, + step-two retest
    cons_liquidity_retest_months_required: int = 4  # of 6
    min_trading_days_per_month: int = 5
    max_nontrading_days_per_year: int = 60

    usd_inr: float = 84.0   # only used for display; no threshold depends on it


CONFIG = Config()
