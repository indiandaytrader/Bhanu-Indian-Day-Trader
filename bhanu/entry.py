"""Opening-range-breakout (ORB) entry planner for NSE day trading.

Original code, written from the rules in the DayTrader study note
(``~/workspace/goals/trading-knowledge-research/files/DAY_TRADER_SKILL_STUDY.md``).
No code was copied from the day-trader-skill repo.

NSE rules applied here
----------------------
* Market hours 09:15-15:30 IST; flat by 15:15 IST.
* Opening range = the first 15 minutes: the 09:15, 09:20 and 09:25 5-minute bars.
* Trigger = the first completed 5-minute bar *after* the range whose CLOSE is
  above the range high (long) or below the range low (short).  A wick past the
  level does not count, and a close exactly at the level is not above/below it.
* No entries before 09:30 IST.  One trigger per setup per day.  A trigger
  after 12:00 IST is flagged ``late_entry=True``.

Paper trading only: nothing here places an order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from .exit import compute_exit

IST = ZoneInfo("Asia/Kolkata")

RANGE_START = dtime(9, 15)   # first range bar starts 09:15 IST
RANGE_END = dtime(9, 30)     # entries only from 09:30 IST onwards
LATE_CUTOFF = dtime(12, 0)   # a trigger after 12:00 IST is a late entry

# Reason for the most recent call that returned None.  Kept as a module
# attribute (rather than an exception) because "no trigger today" is a
# normal, expected outcome, not an error.
LAST_NO_TRIGGER_REASON: Optional[str] = None


@dataclass
class OrbPlan:
    """A planned ORB entry for one symbol on one day."""

    symbol: str
    direction: str                    # "long" or "short"
    range_high: float
    range_low: float
    range_start: datetime             # start of the first range bar (IST)
    trigger_price: float              # close of the trigger bar
    trigger_time: datetime            # start of the trigger bar (IST)
    entry: float                      # entry price (= trigger close)
    stop: Optional[float] = None      # structural stop (range midpoint, or-mid)
    target: Optional[float] = None    # target at the configured R:R (= TP2)
    tp1: Optional[float] = None       # 1R target
    tp2: Optional[float] = None       # 2R target (= target)
    tp3: Optional[float] = None       # 3R target
    status: str = "planned"           # "planned" | "late" | "exit_refused"
    late_entry: bool = False          # True when the trigger came after 12:00


def _normalise_bars(bars: pd.DataFrame) -> pd.DataFrame:
    """Return a copy with a tz-aware IST DatetimeIndex and OHLC columns.

    Accepts either a DatetimeIndex or a ``timestamp`` column.  Naive
    timestamps are treated as IST; aware timestamps are converted to IST.
    """
    df = bars.copy()
    if "timestamp" in df.columns:
        df = df.set_index("timestamp")
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("bars need a DatetimeIndex or a 'timestamp' column")
    if df.index.tz is None:
        df.index = df.index.tz_localize(IST)
    else:
        df.index = df.index.tz_convert(IST)
    for col in ("open", "high", "low", "close"):
        if col not in df.columns:
            raise ValueError(f"bars are missing required column {col!r}")
    return df.sort_index()


def _range_bars(day: pd.DataFrame) -> pd.DataFrame:
    """The three 5-minute bars forming the 09:15-09:30 opening range."""
    return day.between_time(RANGE_START, dtime(9, 25), inclusive="both")


def plan_orb_entry(
    bars: pd.DataFrame,
    direction: str = "long",
    symbol: str = "NIFTY",
    target_rr: float = 2.0,
) -> Optional[OrbPlan]:
    """Plan an ORB entry from one day of 5-minute bars.

    Returns an :class:`OrbPlan` on the first qualifying trigger bar, or
    ``None`` when there is no trigger.  When ``None`` is returned the reason
    is stored in :data:`LAST_NO_TRIGGER_REASON` (and also returned by
    :func:`explain_no_trigger`).

    Parameters
    ----------
    bars:
        5-minute OHLC bars (``open``/``high``/``low``/``close``) with a
        DatetimeIndex or a ``timestamp`` column.  Only the earliest date in
        the frame is used — one plan per day.
    direction:
        ``"long"`` (breakout above the range high) or ``"short"``
        (breakdown below the range low).
    symbol:
        Label carried onto the plan (e.g. ``"NIFTY"``).
    target_rr:
        Reward-to-risk multiple used for the plan's target (default 2.0).
    """
    global LAST_NO_TRIGGER_REASON
    LAST_NO_TRIGGER_REASON = None
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")

    day = _normalise_bars(bars)
    if day.empty:
        LAST_NO_TRIGGER_REASON = "no bars supplied"
        return None
    first_date = day.index[0].date()
    day = day[day.index.date == first_date]
    if day.empty:
        LAST_NO_TRIGGER_REASON = "no bars for the trading day"
        return None

    rng = _range_bars(day)
    if len(rng) < 3:
        LAST_NO_TRIGGER_REASON = (
            f"only {len(rng)} opening-range bar(s); need the 09:15/09:20/09:25 bars"
        )
        return None
    range_high = float(rng["high"].max())
    range_low = float(rng["low"].min())
    range_start = rng.index[0].to_pydatetime()

    # First completed 5m bar after the range: bars starting at/after 09:30.
    after = day[day.index.time >= RANGE_END]
    trigger = None
    for ts, row in after.iterrows():
        close = float(row["close"])
        if direction == "long" and close > range_high:
            trigger = (ts.to_pydatetime(), close)
            break
        if direction == "short" and close < range_low:
            trigger = (ts.to_pydatetime(), close)
            break
    if trigger is None:
        LAST_NO_TRIGGER_REASON = (
            f"no 5m bar closed "
            f"{'above' if direction == 'long' else 'below'} the range "
            f"({'high' if direction == 'long' else 'low'} "
            f"{range_high if direction == 'long' else range_low:.2f})"
        )
        return None

    trigger_time, trigger_price = trigger
    late = trigger_time.time() > LATE_CUTOFF

    plan = OrbPlan(
        symbol=symbol,
        direction=direction,
        range_high=range_high,
        range_low=range_low,
        range_start=range_start,
        trigger_price=trigger_price,
        trigger_time=trigger_time,
        entry=trigger_price,
        status="late" if late else "planned",
        late_entry=late,
    )
    # Stop/target from the default structural rule (range midpoint stop,
    # target at target_rr).  If the exit rules refuse the plan (e.g. the
    # risk is too tight for the spread) keep the trigger but flag it.
    try:
        exit_plan = compute_exit(
            entry=trigger_price,
            range_high=range_high,
            range_low=range_low,
            target_rr=target_rr,
        )
        plan.stop = exit_plan.stop
        plan.target = exit_plan.target
        plan.tp1 = exit_plan.tp1
        plan.tp2 = exit_plan.tp2
        plan.tp3 = exit_plan.tp3
    except ValueError as exc:
        plan.status = "exit_refused"
        plan.stop = None
        plan.target = None
        plan.tp1 = None
        plan.tp2 = None
        plan.tp3 = None
        LAST_NO_TRIGGER_REASON = f"trigger found but exit refused: {exc}"
    return plan


def explain_no_trigger(bars: pd.DataFrame, direction: str = "long") -> str:
    """Return the human-readable reason the last :func:`plan_orb_entry` call
    produced no plan (empty string when a plan was produced)."""
    if plan_orb_entry(bars, direction=direction) is not None:
        return ""
    return LAST_NO_TRIGGER_REASON or "no trigger"


def _demo_bars() -> pd.DataFrame:
    """Synthetic one-day 5m bars (offline demo data, not real prices)."""
    times = pd.date_range("2026-11-26 09:15", periods=12, freq="5min", tz=IST)
    closes = [25700, 25710, 25695, 25705, 25730, 25745, 25760, 25755, 25770, 25780, 25775, 25790]
    rows = []
    for ts, c in zip(times, closes):
        rows.append(
            {"timestamp": ts, "open": c - 5, "high": c + 8, "low": c - 8, "close": c}
        )
    # Opening range 09:15-09:25: high 25718, low 25687.
    rows[0]["high"], rows[0]["low"] = 25708, 25692
    rows[1]["high"], rows[1]["low"] = 25718, 25702
    rows[2]["high"], rows[2]["low"] = 25703, 25687
    # 09:30 bar wicks above the range high but closes below it: no trigger.
    rows[3]["high"], rows[3]["close"] = 25740, 25705
    # 09:35 bar closes above the range high: trigger.
    rows[4]["high"], rows[4]["close"] = 25738, 25730
    return pd.DataFrame(rows)


if __name__ == "__main__":
    demo = _demo_bars()
    for direction in ("long", "short"):
        plan = plan_orb_entry(demo, direction=direction, symbol="NIFTY")
        if plan is None:
            print(f"{direction}: no plan — {LAST_NO_TRIGGER_REASON}")
        else:
            print(f"{direction}: {plan}")
