"""
Incremental daily volume + price refresh. Only pulls days NEW since the
last run - a full 500-symbol history pull takes 30-60 minutes and isn't
needed every run; the panel already has everything up to last time.
"""

import os
import time

import pandas as pd

from ..data_health import check_mass_null_rate, check_volume


def refresh_volume_panel(symbols: list[str], panel_path: str, health_log,
                         sleep_between: float = 0.15) -> pd.DataFrame:
    import yfinance as yf

    if panel_path and os.path.exists(panel_path):
        existing = pd.read_csv(panel_path, parse_dates=["Date"])
        if existing["Date"].dt.tz is not None:
            existing["Date"] = existing["Date"].dt.tz_localize(None)
        last_date = existing["Date"].max()
    else:
        existing = pd.DataFrame(columns=["Date", "symbol", "Close", "Volume"])
        last_date = pd.Timestamp("2021-06-01")   # matches the project's
                                                  # original historical start

    start = (last_date + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    today = pd.Timestamp.now().strftime("%Y-%m-%d")

    if start >= today:
        health_log.record("yfinance_volume", "ALL", "already up to date, "
                          "no new trading days to fetch since last run")
        return existing

    new_rows = []
    n_symbol_failures = 0
    for sym in symbols:
        try:
            hist = yf.Ticker(f"{sym}.NS").history(start=start, auto_adjust=False)
        except Exception:
            n_symbol_failures += 1
            continue
        if hist.empty:
            continue
        idx = hist.index.tz_localize(None) if hist.index.tz is not None else hist.index
        for d, row in zip(idx, hist.itertuples()):
            new_rows.append({"Date": d, "symbol": sym,
                             "Close": row.Close, "Volume": row.Volume})
        time.sleep(sleep_between)

    is_sane, reason = check_mass_null_rate(n_symbol_failures, len(symbols))
    if not is_sane:
        health_log.record("yfinance_volume", "ALL_SYMBOLS", reason)
        # too many outright failures to trust this pull - keep the old panel
        return existing

    new_df = pd.DataFrame(new_rows)
    ok, reason = check_volume(len(new_df), expected_min_rows=max(1, len(symbols) // 3))
    if not ok:
        health_log.record("yfinance_volume", "ALL", reason)
        return existing

    combined = pd.concat([existing, new_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=["Date", "symbol"], keep="last")
    return combined
