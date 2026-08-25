"""
Anomaly detection and last-known-good fallback - the safety net that
makes unattended automation acceptable despite NSE/yfinance being
fragile sources (see README for the specific incidents this project
already hit: wrong field names, silent nulls, rate limiting).

DESIGN PRINCIPLE: a fetch should never be trusted just because it
returned 200 OK. It must pass a sanity check against what we already
know before it's allowed to overwrite the cache. If it fails, we keep
the old value and record why - loudly, in the final report, not just
in a log nobody reads.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class HealthEvent:
    source: str          # "nse_free_float", "nse_shares", "yfinance_volume", etc.
    symbol: str
    reason: str           # e.g. "value out of range", "jump >50% unexplained"
    fell_back_to_date: str | None
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())


class DataHealthLog:
    """Accumulates every fallback/anomaly event during one run, so the
    final report can show a clear, human-readable warning instead of
    silently using stale data."""

    def __init__(self):
        self.events: list[HealthEvent] = []

    def record(self, source: str, symbol: str, reason: str,
              fell_back_to_date: str | None = None):
        self.events.append(HealthEvent(source, symbol, reason, fell_back_to_date))

    @property
    def has_issues(self) -> bool:
        return len(self.events) > 0

    def summary_lines(self) -> list[str]:
        if not self.events:
            return []
        by_source: dict[str, int] = {}
        for e in self.events:
            by_source[e.source] = by_source.get(e.source, 0) + 1
        lines = [f"{count} symbols affected in {source}"
                for source, count in by_source.items()]
        return lines

    def to_dict_list(self) -> list[dict]:
        return [{"source": e.source, "symbol": e.symbol, "reason": e.reason,
                 "fell_back_to_date": e.fell_back_to_date,
                 "timestamp": e.timestamp} for e in self.events]


class LastKnownGoodCache:
    """Simple per-(source, symbol) value cache backed by a JSON file.
    A fetcher checks a new value against this BEFORE accepting it; if
    the new value fails the sanity check, the cached value is returned
    instead and the event is logged."""

    def __init__(self, path: str):
        self.path = path
        self._data: dict = {}
        if os.path.exists(path):
            with open(path) as f:
                self._data = json.load(f)

    def get(self, source: str, symbol: str) -> dict | None:
        return self._data.get(source, {}).get(symbol)

    def set(self, source: str, symbol: str, value: dict, as_of_date: str):
        self._data.setdefault(source, {})[symbol] = {
            **value, "_as_of_date": as_of_date}

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self._data, f, indent=2)


# ==============================================================================
# Sanity checks, one per data type. Each returns (is_sane, reason_if_not).
# Thresholds are deliberately generous - the goal is to catch CLEARLY
# broken data (nulls, 10x errors, impossible values), not to second-guess
# genuine volatility. A real 10x share-count error (TENNIND, INDIAMART -
# both hit during this project's development) is exactly what this is
# built to catch.
# ==============================================================================

def check_free_float(new_value: float | None, prior_value: float | None) -> tuple[bool, str]:
    if new_value is None:
        return False, "null value returned"
    if not (0 <= new_value <= 100):
        return False, f"out of range: {new_value}"
    if prior_value is not None and prior_value > 0:
        # free float can jump on genuine corporate actions (buybacks,
        # QIPs, promoter sales) - only flag truly implausible jumps
        if abs(new_value - prior_value) > 40:
            return False, f"jump of {abs(new_value - prior_value):.1f}pp " \
                          f"vs prior {prior_value}"
    return True, ""


def check_shares(new_value: float | None, prior_value: float | None) -> tuple[bool, str]:
    if new_value is None or new_value <= 0:
        return False, "null or non-positive"
    if prior_value is not None and prior_value > 0:
        ratio = new_value / prior_value
        # splits/bonuses are real and can be 2x-10x; genuine data errors
        # in this project were ALSO exactly 10x, which is why this check
        # cannot simply reject large ratios - it flags them for review
        # rather than silently accepting OR silently rejecting
        if ratio > 15 or ratio < 1 / 15:
            return False, f"share count ratio {ratio:.2f}x vs prior - " \
                          f"could be a real split or a data error, review manually"
    return True, ""


def check_price(new_value: float | None, prior_value: float | None) -> tuple[bool, str]:
    if new_value is None or new_value <= 0:
        return False, "null or non-positive"
    if prior_value is not None and prior_value > 0:
        ratio = new_value / prior_value
        if ratio > 5 or ratio < 0.2:
            return False, f"price ratio {ratio:.2f}x vs prior - implausible " \
                          f"without a known corporate action"
    return True, ""


def check_volume(new_rows: int, expected_min_rows: int) -> tuple[bool, str]:
    """For the incremental volume pull - flags a systemic failure (e.g.
    every symbol returning zero new rows) rather than checking any one
    symbol, since a handful of illiquid names legitimately having no
    new trades is normal."""
    if new_rows < expected_min_rows:
        return False, f"only {new_rows} new rows fetched, expected at least " \
                      f"{expected_min_rows} - possible systemic failure"
    return True, ""


def check_mass_null_rate(n_null: int, n_total: int, max_null_fraction: float = 0.15) -> tuple[bool, str]:
    """If more than max_null_fraction of ALL symbols came back null in one
    pull, that's very unlikely to be genuine data gaps and much more
    likely a broken endpoint (wrong field name, session expired, rate
    limited) - exactly the failure mode that has hit this project before."""
    if n_total == 0:
        return True, ""
    frac = n_null / n_total
    if frac > max_null_fraction:
        return False, f"{n_null}/{n_total} ({frac:.0%}) symbols null - " \
                      f"likely a systemic fetch failure, not real data gaps"
    return True, ""
