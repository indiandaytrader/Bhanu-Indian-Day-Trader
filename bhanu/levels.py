"""Live support / resistance levels + entry/exit display for NSE indices.

What it shows (per index: NIFTY, BANKNIFTY, SENSEX)
---------------------------------------------------
* Current price (last 5m close).
* Nearest support / nearest resistance — the closest level below / above
  the price, drawn from: recent swing highs/lows on 5m bars (last 5
  sessions), previous-day high/low (PDH/PDL), and today's opening-range
  high/low (09:15-09:30 IST).
* PDH / PDL, ORB high / ORB low.
* The ORB signal for today (from ``bhanu.scanner``) with entry, stop
  (range midpoint, with the 1.5x ATR stop alongside) and 2R target
  (from ``bhanu.entry`` / ``bhanu.exit``) — so a trader sees at a glance
  "enter here, exit here, support here, resistance here".

Sources and honesty
-------------------
Levels come from Yahoo Finance 5m bars (public, unofficial, delayed —
a proxy, never trade on this alone; verify on the live platform before
any order). The NSE option chain is often blocked (HTTP 403) for
non-browser clients; this module never fakes chain data and does not
touch the option chain at all. No third-party code is used here.

Paper trading only: nothing here places an order.
"""

from __future__ import annotations

import sys
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from bhanu import entry as entry_mod
from bhanu import exit as exit_mod
from bhanu import scanner as scanner_mod
from bhanu.data import (
    INDICES,
    DataError,
    fetch_daily_bars,
    fetch_intraday_bars,
)

IST = ZoneInfo("Asia/Kolkata")

#: Sessions of 5m bars used for swing highs/lows.
LOOKBACK_SESSIONS = 5
#: Bars either side of a pivot bar for a swing high/low.
SWING_WINDOW = 2
#: ATR period (Wilder's) on 5m bars, for the 1.5x ATR stop.
ATR_PERIOD = 14
#: Levels closer than this share of the price are merged into one.
CLUSTER_TOL_PCT = 0.05


# ---------------------------------------------------------------------------
# Swing highs / lows
# ---------------------------------------------------------------------------

def swing_levels(bars: pd.DataFrame, window: int = SWING_WINDOW) -> tuple[list[float], list[float]]:
    """Pivot swing highs and lows from OHLC bars.

    A swing high is a bar whose high is the maximum over the ``window``
    bars either side of it and strictly above both immediate neighbours;
    a swing low is the mirror image. Returns ``(highs, lows)`` as lists
    of prices, in bar order. Empty bars give empty lists.
    """
    highs: list[float] = []
    lows: list[float] = []
    if bars.empty or len(bars) < 2 * window + 1:
        return highs, lows
    h = bars["high"].to_numpy(dtype=float)
    low = bars["low"].to_numpy(dtype=float)
    n = len(bars)
    for i in range(window, n - window):
        seg_h = h[i - window : i + window + 1]
        seg_l = low[i - window : i + window + 1]
        if h[i] == seg_h.max() and h[i] > h[i - 1] and h[i] > h[i + 1]:
            highs.append(float(h[i]))
        if low[i] == seg_l.min() and low[i] < low[i - 1] and low[i] < low[i + 1]:
            lows.append(float(low[i]))
    return highs, lows


def cluster_levels(levels: list[float], price: float) -> list[float]:
    """Merge levels closer than ``CLUSTER_TOL_PCT`` of ``price`` into one.

    Near-duplicate levels (the same zone hit twice) are replaced by
    their mean, so the level list stays readable. Returns sorted levels.
    """
    if not levels:
        return []
    tol = abs(price) * CLUSTER_TOL_PCT / 100.0
    out: list[float] = []
    group: list[float] = []
    for lv in sorted(levels):
        if group and lv - group[-1] <= tol:
            group.append(lv)
        else:
            if group:
                out.append(sum(group) / len(group))
            group = [lv]
    if group:
        out.append(sum(group) / len(group))
    return out


# ---------------------------------------------------------------------------
# Previous-day high / low, ATR, nearest levels
# ---------------------------------------------------------------------------

def prev_day_high_low(daily_bars: pd.DataFrame) -> tuple[float, float] | None:
    """(PDH, PDL) from the last COMPLETED daily session.

    When the last daily bar is today's (partial) bar, the previous
    completed session is the bar before it. Returns None when there are
    no bars (or only today's partial bar).
    """
    if daily_bars.empty:
        return None
    today = datetime.now(IST).date()
    last_date = daily_bars.index[-1].date()
    if last_date == today:
        if len(daily_bars) < 2:
            return None
        prev = daily_bars.iloc[-2]
    else:
        prev = daily_bars.iloc[-1]
    return float(prev["high"]), float(prev["low"])


def atr_wilder(bars: pd.DataFrame, period: int = ATR_PERIOD) -> float | None:
    """Wilder's ATR (``period`` bars) from OHLC bars, or None if too few."""
    if bars.empty or len(bars) < period + 1:
        return None
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    close = bars["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    val = atr.iloc[-1]
    return float(val) if pd.notna(val) else None


def nearest_levels(price: float, levels: list[float]) -> tuple[float | None, float | None]:
    """(nearest support, nearest resistance) around ``price``.

    Support is the highest level below the price, resistance the lowest
    level above it. Either is None when no level exists on that side.
    """
    below = [lv for lv in levels if lv < price]
    above = [lv for lv in levels if lv > price]
    support = max(below) if below else None
    resistance = min(above) if above else None
    return support, resistance


# ---------------------------------------------------------------------------
# Per-symbol levels + ORB entry/exit
# ---------------------------------------------------------------------------

def compute_levels(
    symbol: str,
    yahoo_symbol: str | None = None,
    lookback_sessions: int = LOOKBACK_SESSIONS,
) -> dict[str, Any]:
    """Live levels + ORB entry/exit for one index.

    Fetches 5m bars (``lookback_sessions`` sessions) and daily bars,
    then returns a dict with: symbol, price, nearest_support,
    nearest_resistance, pdh, pdl, orb_high, orb_low, swing_highs,
    swing_lows, levels (all clustered S/R levels), atr, and the ORB
    signal (``signal``: "long"/"short"/None, ``entry``, ``stop``,
    ``atr_stop``, ``target``). Raises :class:`DataError` when the fetch
    fails — never returns fabricated levels.
    """
    yahoo_symbol = yahoo_symbol or INDICES[symbol]
    bars_5m = fetch_intraday_bars(yahoo_symbol, interval="5m", days=lookback_sessions)
    if bars_5m.empty:
        raise DataError(f"no 5m bars for {symbol}")
    daily = fetch_daily_bars(yahoo_symbol, days=30)

    price = float(bars_5m["close"].iloc[-1])
    today = bars_5m.index[-1].date()
    today_bars = bars_5m[bars_5m.index.date == today]

    swing_highs, swing_lows = swing_levels(bars_5m)
    pdh_pdl = prev_day_high_low(daily)
    pdh, pdl = pdh_pdl if pdh_pdl is not None else (None, None)
    orb = scanner_mod.orb_range(today_bars)
    orb_high, orb_low = orb if orb is not None else (None, None)

    raw_levels: list[float] = list(swing_highs) + list(swing_lows)
    for lv in (pdh, pdl, orb_high, orb_low):
        if lv is not None:
            raw_levels.append(float(lv))
    levels = cluster_levels(raw_levels, price)
    support, resistance = nearest_levels(price, levels)
    atr = atr_wilder(bars_5m)

    # ORB signal + entry/exit (reuse the existing entry/exit rules).
    signal: str | None = None
    entry = stop = atr_stop = target = None
    hit = scanner_mod.orb_hit(today_bars)
    if hit is not None:
        signal = hit["direction"]
        plan = entry_mod.plan_orb_entry(
            today_bars, direction=signal, symbol=symbol
        )
        if plan is not None:
            entry = plan.entry
            stop = plan.stop
            target = plan.target
            if atr is not None and plan.range_high and plan.range_low:
                try:
                    atr_plan = exit_mod.compute_exit(
                        entry=plan.entry,
                        range_high=plan.range_high,
                        range_low=plan.range_low,
                        atr=atr,
                        rule="atr:1.5",
                    )
                    atr_stop = atr_plan.stop
                except ValueError:
                    atr_stop = None

    return {
        "symbol": symbol,
        "price": price,
        "nearest_support": support,
        "nearest_resistance": resistance,
        "pdh": pdh,
        "pdl": pdl,
        "orb_high": orb_high,
        "orb_low": orb_low,
        "swing_highs": swing_highs,
        "swing_lows": swing_lows,
        "levels": levels,
        "atr": atr,
        "signal": signal,
        "entry": entry,
        "stop": stop,
        "atr_stop": atr_stop,
        "target": target,
        "date": str(today),
    }


def scan_levels(
    universe: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Live levels for the three indices (default) or ``universe``.

    Returns ``{"run_at_ist", "levels": [...], "failed": [...]}`` — a
    symbol that fails to fetch is listed in ``failed``, never silently
    shown as "no levels".
    """
    universe = universe or INDICES
    rows: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    for name, yahoo_symbol in universe.items():
        try:
            rows.append(compute_levels(name, yahoo_symbol))
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            failed.append({"symbol": name, "reason": str(exc)})
    return {
        "run_at_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        "levels": rows,
        "failed": failed,
    }


def main() -> int:
    result = scan_levels()
    print(f"levels — {result['run_at_ist']}")
    for row in result["levels"]:
        sup = row["nearest_support"]
        res = row["nearest_resistance"]
        print(
            f"  {row['symbol']}: price {row['price']:,.2f} | "
            f"support {sup:,.2f} | resistance {res:,.2f} | "
            f"PDH {row['pdh']:,.2f} | PDL {row['pdl']:,.2f} | "
            f"ORB {row['orb_low']:,.2f}-{row['orb_high']:,.2f} | "
            f"signal {row['signal'] or 'None'} "
            f"entry {row['entry']} SL {row['stop']} TP {row['target']}"
        )
    for item in result["failed"]:
        print(f"  FAILED {item['symbol']}: {item['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
