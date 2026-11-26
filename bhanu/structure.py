"""Market structure from OHLC bars: VWAP, swings, CHoCH/BOS, order blocks, FVG.

All functions are pure (bars in, facts out) and work on synthetic bars,
so they are unit-tested offline with no network. They only describe what
the bars actually show — a level that is not in the data is never
invented. The dashboard draws these as overlays on the candlestick
chart; anything a function does not find is simply not drawn.

Definitions used here (standard smart-money / structure reading):
* VWAP — cumulative (typical price x volume) / cumulative volume,
  reset each session (intraday).
* Swing high / low — a bar whose high (low) is the extreme over
  ``window`` bars either side and strictly beyond both neighbours.
* CHoCH / BOS — a CLOSE beyond the most recent confirmed swing high
  (bullish break) or swing low (bearish break). It is a CHoCH when it
  reverses the previous trend, a BOS when it continues it. The initial
  trend comes from the order of the first swing low/high pair.
* Order block — the last counter-trend candle before the impulse leg
  that caused a break: the last down candle before a bullish break
  (bullish OB), the last up candle before a bearish break (bearish OB).
  The zone is that candle's [low, high].
* Fair value gap (FVG) — a 3-candle gap: bullish when bar[i-2].high <
  bar[i].low (gap = [bar[i-2].high, bar[i].low]); bearish when
  bar[i-2].low > bar[i].high. Only gaps the price has not traded back
  into since (unfilled) are returned.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

#: Bars either side of a pivot bar for a swing high/low.
SWING_WINDOW = 2
#: How far back to look for the order-block candle before a break.
OB_LOOKBACK_BARS = 20
#: Most recent unfilled FVGs / order blocks returned (keeps charts readable).
MAX_OVERLAYS = 3


def _unix(ts) -> int:
    """A pandas Timestamp (or datetime) as a unix-seconds int."""
    return int(pd.Timestamp(ts).timestamp())


def vwap(bars: pd.DataFrame) -> pd.Series:
    """Session VWAP from OHLCV bars (typical price x volume, cumulative).

    Returns a Series aligned to ``bars`` (NaN where volume is missing).
    A single session only — pass one day of bars; the dashboard resets
    it per session by construction.
    """
    if bars.empty:
        return pd.Series(dtype=float)
    typical = (bars["high"].astype(float) + bars["low"].astype(float) + bars["close"].astype(float)) / 3.0
    volume = bars["volume"].astype(float).fillna(0.0)
    cum_pv = (typical * volume).cumsum()
    cum_v = volume.cumsum()
    # Where cumulative volume is 0 (index bars carry no volume at all),
    # VWAP is undefined — NaN, never an error. np.nan (not pd.NA) so the
    # result always casts cleanly to float.
    out = cum_pv / cum_v.where(cum_v > 0)
    return out.astype(float)


def swing_points(bars: pd.DataFrame, window: int = SWING_WINDOW) -> list[dict[str, Any]]:
    """Swing highs/lows in time order: ``{time, price, kind}``.

    A swing high is a bar whose high is the maximum over ``window``
    bars either side and strictly above both immediate neighbours;
    a swing low is the mirror image. ``time`` is a unix-seconds int.
    """
    points: list[dict[str, Any]] = []
    if bars.empty or len(bars) < 2 * window + 1:
        return points
    h = bars["high"].to_numpy(dtype=float)
    low = bars["low"].to_numpy(dtype=float)
    n = len(bars)
    for i in range(window, n - window):
        seg_h = h[i - window : i + window + 1]
        seg_l = low[i - window : i + window + 1]
        if h[i] == seg_h.max() and h[i] > h[i - 1] and h[i] > h[i + 1]:
            points.append({"time": _unix(bars.index[i]), "price": float(h[i]), "kind": "high"})
        if low[i] == seg_l.min() and low[i] < low[i - 1] and low[i] < low[i + 1]:
            points.append({"time": _unix(bars.index[i]), "price": float(low[i]), "kind": "low"})
    points.sort(key=lambda p: p["time"])
    return points


def structure_breaks(bars: pd.DataFrame, window: int = SWING_WINDOW) -> list[dict[str, Any]]:
    """CHoCH / BOS breaks: closes beyond the most recent confirmed swing.

    A swing at bar ``i`` is only "confirmed" once ``window`` bars have
    passed (bar ``i + window``), so a break is only ever judged against
    swings the market had actually printed by that bar — no lookahead.
    Returns ``{time, price, kind, direction}`` in time order, where
    ``kind`` is "CHoCH" (reversal of the previous trend) or "BOS"
    (continuation), and ``direction`` is "bullish" or "bearish".
    """
    breaks: list[dict[str, Any]] = []
    if bars.empty or len(bars) < 2 * window + 1:
        return breaks
    points = swing_points(bars, window=window)
    if not points:
        return breaks
    # Initial trend from the order of the first swing low/high pair.
    first_high = next((p for p in points if p["kind"] == "high"), None)
    first_low = next((p for p in points if p["kind"] == "low"), None)
    trend: str | None = None
    if first_high and first_low:
        trend = "up" if first_low["time"] < first_high["time"] else "down"

    closes = bars["close"].to_numpy(dtype=float)
    index = bars.index
    # Swing confirmation bar for the swing at position k in `points`.
    # A swing at bar i is confirmed at bar i + window; find i by time.
    times = [_unix(t) for t in index]
    time_to_pos = {t: k for k, t in enumerate(times)}

    last_high: float | None = None
    last_low: float | None = None
    seen = 0  # swings confirmed so far
    for j in range(len(bars)):
        # Confirm any swing whose confirmation bar is this bar or earlier.
        while seen < len(points):
            p = points[seen]
            confirm_pos = time_to_pos.get(p["time"], 0) + window
            if confirm_pos > j:
                break
            if p["kind"] == "high":
                last_high = p["price"]
            else:
                last_low = p["price"]
            seen += 1
        close = float(closes[j])
        if last_high is not None and close > last_high:
            direction = "bullish"
            kind = "CHoCH" if trend == "down" else "BOS"
            breaks.append(
                {"time": times[j], "price": close, "kind": kind, "direction": direction}
            )
            trend = "up"
            last_high = None  # consumed — wait for the next swing high
        elif last_low is not None and close < last_low:
            direction = "bearish"
            kind = "CHoCH" if trend == "up" else "BOS"
            breaks.append(
                {"time": times[j], "price": close, "kind": kind, "direction": direction}
            )
            trend = "down"
            last_low = None
    return breaks


def order_blocks(
    bars: pd.DataFrame,
    breaks: list[dict[str, Any]] | None = None,
    lookback: int = OB_LOOKBACK_BARS,
    limit: int = MAX_OVERLAYS,
) -> list[dict[str, Any]]:
    """Order-block zones for the given breaks (most recent first).

    For a bullish break, the OB is the last down candle (close < open)
    in the ``lookback`` bars before the break bar; for a bearish break
    the last up candle. The zone is that candle's [low, high] and it
    runs from the candle's time to the last bar. Returns
    ``{kind, top, bottom, from_time, label}``. A break with no
    counter-trend candle in the lookback simply gets no block.
    """
    if bars.empty:
        return []
    if breaks is None:
        breaks = structure_breaks(bars)
    opens = bars["open"].to_numpy(dtype=float)
    highs = bars["high"].to_numpy(dtype=float)
    lows = bars["low"].to_numpy(dtype=float)
    closes = bars["close"].to_numpy(dtype=float)
    times = [_unix(t) for t in bars.index]
    time_to_pos = {t: k for k, t in enumerate(times)}
    out: list[dict[str, Any]] = []
    for br in reversed(breaks):
        pos = time_to_pos.get(br["time"])
        if pos is None:
            continue
        start = max(0, pos - lookback)
        found = None
        if br["direction"] == "bullish":
            for k in range(pos - 1, start - 1, -1):
                if closes[k] < opens[k]:
                    found = k
                    break
        else:
            for k in range(pos - 1, start - 1, -1):
                if closes[k] > opens[k]:
                    found = k
                    break
        if found is None:
            continue
        out.append(
            {
                "kind": "bullish" if br["direction"] == "bullish" else "bearish",
                "top": float(highs[found]),
                "bottom": float(lows[found]),
                "from_time": times[found],
                "label": "Bullish OB" if br["direction"] == "bullish" else "Bearish OB",
            }
        )
        if len(out) >= limit:
            break
    return out


def fair_value_gaps(
    bars: pd.DataFrame, limit: int = MAX_OVERLAYS
) -> list[dict[str, Any]]:
    """Most recent UNFILLED fair value gaps (3-candle gaps).

    Bullish: bar[i-2].high < bar[i].low — gap [bar[i-2].high, bar[i].low].
    Bearish: bar[i-2].low > bar[i].high — gap [bar[i].high, bar[i-2].low].
    A gap is "filled" (and dropped) when any later bar trades back
    into it: a later low <= gap top (bullish) or a later high >= gap
    bottom (bearish). Returns ``{kind, top, bottom, from_time, label}``,
    most recent first.
    """
    if bars.empty or len(bars) < 3:
        return []
    highs = bars["high"].to_numpy(dtype=float)
    lows = bars["low"].to_numpy(dtype=float)
    times = [_unix(t) for t in bars.index]
    out: list[dict[str, Any]] = []
    for i in range(len(bars) - 1, 1, -1):
        gap = None
        if highs[i - 2] < lows[i]:
            gap = {
                "kind": "bullish",
                "top": float(lows[i]),
                "bottom": float(highs[i - 2]),
                "from_time": times[i],
                "label": "Bullish FVG",
            }
            filled = any(lows[j] <= gap["top"] for j in range(i + 1, len(bars)))
        elif lows[i - 2] > highs[i]:
            gap = {
                "kind": "bearish",
                "top": float(lows[i - 2]),
                "bottom": float(highs[i]),
                "from_time": times[i],
                "label": "Bearish FVG",
            }
            filled = any(highs[j] >= gap["bottom"] for j in range(i + 1, len(bars)))
        else:
            continue
        if filled:
            continue
        out.append(gap)
        if len(out) >= limit:
            break
    return out


def session_bars_today(bars: pd.DataFrame) -> pd.DataFrame:
    """Only today's bars (last date in the frame)."""
    if bars.empty:
        return bars
    today = bars.index[-1].date()
    return bars[bars.index.date == today]


def main() -> int:
    from bhanu.data import make_synthetic_bars

    bars = make_synthetic_bars(n=60)
    print(f"bars: {len(bars)}")
    vw = vwap(bars)
    print(f"VWAP last: {vw.iloc[-1]:.2f}")
    print(f"swings: {len(swing_points(bars))}")
    print(f"breaks: {structure_breaks(bars)}")
    print(f"order blocks: {order_blocks(bars)}")
    print(f"FVGs: {fair_value_gaps(bars)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
