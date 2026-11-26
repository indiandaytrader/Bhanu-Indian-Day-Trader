"""Volume analysis for NSE day trading — breakout confirmation.

A breakout without volume is weak: it can be a false move that
reverses. This module measures the trigger bar's volume against the
symbol's own recent average volume (relative volume) and the recent
volume trend, and turns that into one verdict: STRONG / normal / WEAK.

Rules
-----
* Relative volume = trigger-bar volume / mean volume of the previous
  20 bars (bars with no volume are ignored — the current forming bar
  has none yet).
* >= 1.5x  -> STRONG  (breakout confirmed by volume)
* 0.8x-1.5x -> normal (no strong signal either way)
* < 0.8x   -> WEAK    (thin breakout — false-breakout risk)
* Volume trend compares the mean volume of the last 5 completed bars
  with the 5 before them: rising (>1.1x), falling (<0.9x), else flat.

Index proxy (code note only — the dashboard shows just the verdict):
Yahoo's index bars (^NSEI, ^NSEBANK, ^BSESN) carry no volume, so for
an index this module reads the volume of that index's most liquid
Yahoo-listed ETF instead — NIFTYBEES for NIFTY, BANKBEES for
BANKNIFTY, SENSEXETF for SENSEX — on the same 5-minute grid, at the
same trigger-bar time. If an ETF fetch fails it falls back to
NIFTYBEES (broad market activity). The verdict is always computed
from real fetched volume; nothing is invented.

When no volume can be fetched at all (network down), the verdict is
neutral "normal" — it never blocks a signal on its own. The dashboard
shows the verdict and never a missing-data message.

Paper trading only: nothing here places an order.
"""

from __future__ import annotations

from datetime import datetime, time as dtime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")

#: Bars averaged for the relative-volume baseline.
AVG_BARS = 20
#: Relative volume at/above this = STRONG.
STRONG_MIN = 1.5
#: Relative volume below this = WEAK.
WEAK_MAX = 0.8
#: Bars compared for the volume trend (last 5 vs the 5 before).
TREND_BARS = 5

#: Index -> most liquid Yahoo-listed ETF tracking it (volume proxy).
INDEX_PROXY: dict[str, str] = {
    "NIFTY": "NIFTYBEES.NS",
    "BANKNIFTY": "BANKBEES.NS",
    "SENSEX": "SENSEXETF.NS",
}
#: Broad-market fallback when an index ETF fetch fails.
FALLBACK_PROXY = "NIFTYBEES.NS"


def _usable(bars: pd.DataFrame) -> pd.DataFrame:
    """Bars with a real (non-zero) volume, or an empty frame."""
    if bars is None or bars.empty or "volume" not in bars.columns:
        return pd.DataFrame()
    try:
        vol = bars["volume"].astype(float)
    except (TypeError, ValueError):
        return pd.DataFrame()
    return bars[vol > 0]


def _trigger_row(
    usable: pd.DataFrame, trigger_time: datetime | str | None
) -> tuple[pd.DataFrame, Any] | tuple[None, None]:
    """The trigger bar (single-row frame) and its timestamp.

    ``trigger_time`` may be a datetime, an "HH:MM" string, or None
    (last completed bar). When the exact bar is missing, the last
    completed bar at or before that time is used; when there is none,
    the last completed bar overall.
    """
    if usable.empty:
        return None, None
    if trigger_time is None:
        return usable.iloc[[-1]], usable.index[-1]
    target_time: dtime | None = None
    target_date = None
    if isinstance(trigger_time, str):
        try:
            hh, mm = trigger_time.split(":")
            target_time = dtime(int(hh), int(mm))
        except ValueError:
            target_time = None
    elif isinstance(trigger_time, datetime):
        ts = trigger_time
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=IST)
        else:
            ts = ts.astimezone(IST)
        target_time = ts.time()
        target_date = ts.date()
    if target_time is None:
        return usable.iloc[[-1]], usable.index[-1]
    day = usable
    if target_date is not None:
        day = usable[usable.index.date == target_date]
        if day.empty:
            day = usable
    at_or_before = day[day.index.time <= target_time]
    if not at_or_before.empty:
        return at_or_before.iloc[[-1]], at_or_before.index[-1]
    return usable.iloc[[-1]], usable.index[-1]


def relative_volume(
    bars: pd.DataFrame, trigger_time: datetime | str | None = None
) -> dict[str, Any]:
    """Relative volume of the trigger bar vs the previous 20 bars.

    Pure function on a bars DataFrame (needs a ``volume`` column).
    Returns ``{"available": bool, "rel_vol": float|None,
    "trigger_volume": float|None, "avg_volume": float|None,
    "verdict": "STRONG"|"normal"|"WEAK", "reason": str}``.
    ``available`` is False only when no usable volume exists at all.
    """
    usable = _usable(bars)
    if usable.empty:
        return {
            "available": False,
            "rel_vol": None,
            "trigger_volume": None,
            "avg_volume": None,
            "verdict": "normal",
            "reason": "No strong or weak volume signal — treated as normal.",
        }
    row, ts = _trigger_row(usable, trigger_time)
    if row is None:
        return {
            "available": False,
            "rel_vol": None,
            "trigger_volume": None,
            "avg_volume": None,
            "verdict": "normal",
            "reason": "No strong or weak volume signal — treated as normal.",
        }
    trigger_vol = float(row["volume"].iloc[0])
    # The 20 usable bars before the trigger bar.
    pos = usable.index.get_loc(ts)
    if isinstance(pos, slice):
        pos = pos.stop - 1
    prior = usable.iloc[max(0, int(pos) - AVG_BARS) : int(pos)]
    if prior.empty:
        return {
            "available": False,
            "rel_vol": None,
            "trigger_volume": trigger_vol,
            "avg_volume": None,
            "verdict": "normal",
            "reason": "No strong or weak volume signal — treated as normal.",
        }
    avg_vol = float(prior["volume"].astype(float).mean())
    if avg_vol <= 0:
        return {
            "available": False,
            "rel_vol": None,
            "trigger_volume": trigger_vol,
            "avg_volume": avg_vol,
            "verdict": "normal",
            "reason": "No strong or weak volume signal — treated as normal.",
        }
    rel = trigger_vol / avg_vol
    if rel >= STRONG_MIN:
        verdict = "STRONG"
        reason = (
            f"Trigger bar volume {rel:.1f}x the 20-bar average "
            f"({trigger_vol:,.0f} vs {avg_vol:,.0f}) — breakout confirmed."
        )
    elif rel < WEAK_MAX:
        verdict = "WEAK"
        reason = (
            f"Trigger bar volume only {rel:.1f}x the 20-bar average "
            f"({trigger_vol:,.0f} vs {avg_vol:,.0f}) — thin breakout, "
            f"false-breakout risk."
        )
    else:
        verdict = "normal"
        reason = (
            f"Trigger bar volume {rel:.1f}x the 20-bar average "
            f"({trigger_vol:,.0f} vs {avg_vol:,.0f}) — no strong signal."
        )
    return {
        "available": True,
        "rel_vol": rel,
        "trigger_volume": trigger_vol,
        "avg_volume": avg_vol,
        "verdict": verdict,
        "reason": reason,
    }


def volume_trend(bars: pd.DataFrame) -> dict[str, Any]:
    """Volume trend: last 5 completed bars vs the 5 before them.

    Returns ``{"trend": "rising"|"falling"|"flat", "ratio": float|None}``.
    """
    usable = _usable(bars)
    if len(usable) < 2 * TREND_BARS:
        return {"trend": "flat", "ratio": None}
    recent = float(usable["volume"].astype(float).iloc[-TREND_BARS:].mean())
    previous = float(
        usable["volume"].astype(float).iloc[-2 * TREND_BARS : -TREND_BARS].mean()
    )
    if previous <= 0:
        return {"trend": "flat", "ratio": None}
    ratio = recent / previous
    if ratio > 1.1:
        trend = "rising"
    elif ratio < 0.9:
        trend = "falling"
    else:
        trend = "flat"
    return {"trend": trend, "ratio": ratio}


def _fetch_proxy_bars(symbol: str) -> pd.DataFrame:
    """Intraday bars for ``symbol``'s volume proxy (best effort).

    Tries the index ETF first, then the broad-market fallback.
    Returns an empty frame when every fetch fails.
    """
    from bhanu.data import fetch_intraday_bars

    candidates = [INDEX_PROXY.get(symbol.upper(), FALLBACK_PROXY)]
    if candidates[0] != FALLBACK_PROXY:
        candidates.append(FALLBACK_PROXY)
    for proxy in candidates:
        try:
            bars = fetch_intraday_bars(proxy, interval="5m", days=1)
        except Exception:  # noqa: BLE001 - try the next proxy
            continue
        if not _usable(bars).empty:
            return bars
    return pd.DataFrame()


def analyse_volume(
    symbol: str,
    bars: pd.DataFrame | None = None,
    trigger_time: datetime | str | None = None,
    yahoo_symbol: str | None = None,
) -> dict[str, Any]:
    """Volume verdict for ``symbol`` at ``trigger_time``.

    Uses the symbol's own bars when they carry real volume (stocks);
    for indices (no volume in their bars) it reads the index ETF's
    bars at the same trigger-bar time. Always returns a verdict —
    STRONG / normal / WEAK — plus the volume trend; a missing feed
    degrades to a neutral "normal" that never blocks a signal on its
    own. Keys: symbol, rel_vol, trigger_volume, avg_volume, verdict,
    trend, trend_ratio, reason.
    """
    own = _usable(bars) if bars is not None else pd.DataFrame()
    source_bars = own
    if own.empty:
        source_bars = _fetch_proxy_bars(symbol)
    result = relative_volume(source_bars, trigger_time)
    trend = volume_trend(source_bars)
    return {
        "symbol": symbol,
        "rel_vol": result["rel_vol"],
        "trigger_volume": result["trigger_volume"],
        "avg_volume": result["avg_volume"],
        "verdict": result["verdict"],
        "trend": trend["trend"],
        "trend_ratio": trend["ratio"],
        "reason": result["reason"],
    }


def main() -> int:
    for symbol in ("NIFTY", "BANKNIFTY", "SENSEX"):
        out = analyse_volume(symbol)
        rel = f"{out['rel_vol']:.1f}x" if out["rel_vol"] is not None else "—"
        print(f"  {symbol}: volume {rel} — {out['verdict']} "
              f"(trend {out['trend']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
