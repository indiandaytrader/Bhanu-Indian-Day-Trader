# bhanu/scanner.py
"""Scanner: daily setups (closes only) and the intraday ORB scan.

Daily setups (computed from daily closes only):
    breakout_20d    last close above all prior 20 closes.
    bull_flag       +10% over the 10 sessions before the last 5, then the
                    last 5 closes inside a 5% range.
    reclaim_ma20    previous close under the 20-day average, last close
                    above it.
    oversold        RSI-14 (Wilder) below 30.
    oversold_bounce RSI-14 below 30 yesterday, closed up today.

ORB scan (NSE, 09:15-09:30 IST opening range):
    Range = the first 15 minutes of 5m bars (09:15, 09:20, 09:25).
    A hit is the first completed 5m bar AFTER the range that CLOSES
    above the range high (long) or below the range low (short).
    A wick through the level does not count; a close exactly at the
    level does not count. No entries before 09:30 IST.

CLI:
    python -m bhanu.scanner daily   # pre-market daily setups
    python -m bhanu.scanner orb     # intraday ORB scan (after 09:30 IST)

Both commands print counts (fetched / usable / hits) and the top hits
ranked by price x volume, and write JSON to data/watchlist.json.
Partial results are reported honestly: symbols that failed to fetch
are listed, never silently counted as "no setup".
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from bhanu.data import (
    IST,
    UNIVERSE,
    fetch_daily_bars_safe,
    fetch_intraday_bars,
    make_synthetic_bars,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WATCHLIST_PATH = REPO_ROOT / "data" / "watchlist.json"

# datetime.now() needs a real tzinfo object. bhanu.data.IST is the plain
# string "Asia/Kolkata" (fine for pandas) — passing it to datetime.now()
# raises TypeError and the whole ORB scan dies silently in the poller,
# leaving /api/orb stuck on "scan warming up" forever.
_IST_TZ = ZoneInfo("Asia/Kolkata")

ORB_RANGE_START = "09:15"
ORB_RANGE_END = "09:30"  # range bars are 09:15, 09:20, 09:25 (5m bars)

#: Minimum relative volume on the breakout bar (vs. prior session bars' average)
#: required for ``trade_ready`` confirmation.
TRADE_READY_MIN_REL_VOL = 1.2

#: Tolerance (as a fraction of price) for detecting a pullback retest of the
#: ORB breakout level.
RETEST_TOL_PCT = 0.0015

DAILY_SETUPS = (
    "breakout_20d",
    "bull_flag",
    "reclaim_ma20",
    "oversold",
    "oversold_bounce",
)


# ---------------------------------------------------------------------------
# Indicators (closes only)
# ---------------------------------------------------------------------------

def rsi_wilder(closes: pd.Series, period: int = 14) -> pd.Series:
    """RSI with Wilder's smoothing (alpha = 1/period)."""
    delta = closes.diff()
    gains = delta.clip(lower=0.0)
    losses = (-delta).clip(lower=0.0)
    avg_gain = gains.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = losses.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    # Flat prices (no losses at all) -> RSI 100; no gains -> 0.
    rsi = rsi.fillna(100.0 * (avg_gain > 0).astype(float))
    return rsi


# ---------------------------------------------------------------------------
# Daily setups
# ---------------------------------------------------------------------------

def daily_setups(bars: pd.DataFrame) -> dict[str, bool]:
    """Compute the five daily setups from a daily-bars DataFrame.

    Needs at least 21 closes for breakout_20d / reclaim_ma20 and at
    least 15 closes for bull_flag; RSI needs 15 closes. Missing data
    means the setup is False, never guessed.
    """
    closes = bars["close"].dropna()
    result: dict[str, bool] = {name: False for name in DAILY_SETUPS}
    if len(closes) < 15:
        return result

    last = float(closes.iloc[-1])
    prev = float(closes.iloc[-2])

    # breakout_20d: close above all prior 20 closes.
    if len(closes) >= 21:
        result["breakout_20d"] = bool(last > closes.iloc[-21:-1].max())

    # bull_flag: +10% over the 10 sessions before the last 5, then the
    # last 5 closes inside a 5% range.
    if len(closes) >= 15:
        run_start = float(closes.iloc[-15])
        run_end = float(closes.iloc[-6])
        tail = closes.iloc[-5:]
        run_gain = (run_end - run_start) / run_start if run_start else 0.0
        tail_range = (tail.max() - tail.min()) / tail.min() if tail.min() else 0.0
        result["bull_flag"] = bool(run_gain >= 0.10 and tail_range <= 0.05)

    # reclaim_ma20: prev close under the 20-day average, last close above.
    if len(closes) >= 21:
        ma20_prev = float(closes.iloc[-21:-1].mean())
        ma20_now = float(closes.iloc[-20:].mean())
        result["reclaim_ma20"] = bool(prev < ma20_prev and last > ma20_now)

    # oversold / oversold_bounce on RSI-14 (Wilder).
    rsi = rsi_wilder(closes)
    if len(rsi.dropna()) >= 2:
        result["oversold"] = bool(rsi.iloc[-1] < 30)
        result["oversold_bounce"] = bool(rsi.iloc[-2] < 30 and last > prev)
    return result


def scan_daily(
    universe: dict[str, str] | None = None, days: int = 60
) -> dict[str, Any]:
    """Run the daily scan over ``universe`` (default: full NSE universe).

    Returns a dict with counts, per-symbol setups, and the symbols that
    failed to fetch — so a caller can see partial coverage honestly.
    """
    universe = universe or UNIVERSE
    hits: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    usable = 0
    for name, symbol in universe.items():
        bars, reason = fetch_daily_bars_safe(symbol, days=days)
        if bars.empty:
            failed.append({"symbol": name, "reason": reason})
            continue
        usable += 1
        setups = daily_setups(bars)
        matched = [k for k, v in setups.items() if v]
        if not matched:
            continue
        last = bars.iloc[-1]
        hits.append(
            {
                "symbol": name,
                "yahoo_symbol": symbol,
                "setups": matched,
                "close": float(last["close"]),
                "volume": float(last["volume"]),
                "price_x_volume": float(last["close"]) * float(last["volume"]),
                "date": str(bars.index[-1].date()),
            }
        )
    hits.sort(key=lambda h: h["price_x_volume"], reverse=True)
    return {
        "scan": "daily",
        "run_at_ist": datetime.now().strftime("%Y-%m-%d %H:%M:%S IST"),
        "fetched": len(universe),
        "usable": usable,
        "failed": failed,
        "hits": hits,
        "hit_count": len(hits),
    }


# ---------------------------------------------------------------------------
# ORB scan
# ---------------------------------------------------------------------------

def orb_range(bars: pd.DataFrame) -> tuple[float, float] | None:
    """Opening-range high/low from the first 15 minutes of 5m bars.

    Range bars are the 09:15, 09:20 and 09:25 bars (IST). Returns
    ``(range_high, range_low)`` or None if those bars are missing.
    """
    if bars.empty:
        return None
    today = bars.index[-1].date()
    day_bars = bars[bars.index.date == today]
    range_bars = day_bars.between_time(ORB_RANGE_START, "09:25")
    if len(range_bars) < 3:
        return None
    return float(range_bars["high"].max()), float(range_bars["low"].min())


def _evaluate_breakout_readiness(
    day_bars: pd.DataFrame,
    breakout_ts: Any,
    direction: str,
    range_high: float,
    range_low: float,
) -> tuple[bool, str]:
    """Compute ``(trade_ready, signal_state)`` for a confirmed ORB breakout.

    Exact ``trade_ready`` condition (ALL THREE must hold):
    1. **Clean breakout close**: the breakout bar closed strictly outside
       the 09:15-09:30 opening range (``close > range_high`` for long,
       ``close < range_low`` for short).
    2. **Volume confirmation**: the breakout bar's volume is at least
       ``TRADE_READY_MIN_REL_VOL`` (1.2x) of the average volume of the
       prior bars in today's session (requires positive volume data).
    3. **Follow-through / retest hold**: at least one completed 5m bar
       AFTER the breakout bar has closed and the latest close still holds
       outside the opening range without invalidating through the range
       midpoint (either continuing in the breakout direction or retesting
       the broken level and holding).

    ``signal_state`` values:
    * ``"invalidated"`` — a subsequent bar closed back past the range
      midpoint against the breakout direction.
    * ``"confirmed"`` — ``trade_ready`` is True (volume + follow-through).
    * ``"retested"`` — a subsequent bar pulled back to within
      ``RETEST_TOL_PCT`` (0.15%) of the breakout level and held outside.
    * ``"new"`` — fresh breakout with no follow-through bar yet or
      unconfirmed volume.
    """
    if day_bars.empty or breakout_ts not in day_bars.index:
        return False, "new"

    loc = day_bars.index.get_loc(breakout_ts)
    if isinstance(loc, slice):
        loc = loc.start
    elif not isinstance(loc, int):
        loc = int(loc[0])

    brk_bar = day_bars.iloc[loc]
    brk_close = float(brk_bar["close"])
    mid = (range_high + range_low) / 2.0

    # 1. Clean breakout close outside the opening range.
    clean_break = (
        brk_close > range_high if direction == "long" else brk_close < range_low
    )
    if not clean_break:
        return False, "new"

    # 2. Volume confirmation vs. prior bars in today's session.
    vol_ok = False
    if "volume" in day_bars.columns and loc > 0:
        brk_vol = float(brk_bar["volume"]) if pd.notna(brk_bar["volume"]) else 0.0
        prior_vols = day_bars.iloc[:loc]["volume"].dropna()
        prior_vols = prior_vols[prior_vols > 0]
        if brk_vol > 0 and not prior_vols.empty:
            avg_vol = float(prior_vols.mean())
            vol_ok = avg_vol > 0 and (brk_vol / avg_vol) >= TRADE_READY_MIN_REL_VOL

    # 3. Subsequent bar progression: invalidation, retest, or hold/continuation.
    after_bars = day_bars.iloc[loc + 1 :]
    if after_bars.empty:
        return False, "new"

    retested = False
    for _, bar in after_bars.iterrows():
        c = float(bar["close"])
        lo = float(bar["low"])
        hi = float(bar["high"])
        if direction == "long":
            if c < mid:
                return False, "invalidated"
            if lo <= range_high * (1.0 + RETEST_TOL_PCT) and c > range_high:
                retested = True
        else:
            if c > mid:
                return False, "invalidated"
            if hi >= range_low * (1.0 - RETEST_TOL_PCT) and c < range_low:
                retested = True

    last_close = float(after_bars.iloc[-1]["close"])
    holds_outside = (
        last_close > range_high if direction == "long" else last_close < range_low
    )

    trade_ready = bool(clean_break and vol_ok and holds_outside)
    if trade_ready:
        return True, "confirmed"
    if retested:
        return False, "retested"
    return False, "new"


def orb_hit(bars: pd.DataFrame) -> dict[str, Any] | None:
    """Find the status of ORB for the given bars.

    Returns a dict with ``state`` (``"scanned"`` or ``"breakout"``),
    ``direction``, ``trade_ready``, ``signal_state``, ``timestamp_ist``,
    etc., or ``None`` if no opening range exists yet.
    """
    rng = orb_range(bars)
    if rng is None:
        return None
    range_high, range_low = rng
    today = bars.index[-1].date()
    day_bars = bars[bars.index.date == today]
    after = day_bars.between_time(ORB_RANGE_END, "15:30")

    # The 09:30-stamped bar is the first completed bar after the range.
    for ts, bar in after.iterrows():
        close = float(bar["close"])
        if close > range_high:
            trade_ready, sig_state = _evaluate_breakout_readiness(
                day_bars, ts, "long", range_high, range_low
            )
            return {
                "state": "breakout",
                "direction": "long",
                "close": close,
                "bar_time": ts.strftime("%H:%M"),
                "range_high": range_high,
                "range_low": range_low,
                "trade_ready": trade_ready,
                "signal_state": sig_state,
                "timestamp_ist": ts.strftime("%Y-%m-%d %H:%M:%S IST"),
            }
        if close < range_low:
            trade_ready, sig_state = _evaluate_breakout_readiness(
                day_bars, ts, "short", range_high, range_low
            )
            return {
                "state": "breakout",
                "direction": "short",
                "close": close,
                "bar_time": ts.strftime("%H:%M"),
                "range_high": range_high,
                "range_low": range_low,
                "trade_ready": trade_ready,
                "signal_state": sig_state,
                "timestamp_ist": ts.strftime("%Y-%m-%d %H:%M:%S IST"),
            }

    # Range established but no breakout yet.
    return {
        "state": "scanned",
        "direction": None,
        "range_high": range_high,
        "range_low": range_low,
        "trade_ready": False,
        "signal_state": None,
        "timestamp_ist": datetime.now(_IST_TZ).strftime("%Y-%m-%d %H:%M:%S IST"),
    }


def scan_orb(universe: dict[str, str] | None = None) -> dict[str, Any]:
    """Run the ORB scan over ``universe`` (default: full NSE universe)."""
    universe = universe or UNIVERSE
    hits: list[dict[str, Any]] = []
    scanned: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    usable = 0
    now_ist = datetime.now(_IST_TZ)

    # Global state for the scan.
    scan_state = "scanned"
    if now_ist.hour < 9 or (now_ist.hour == 9 and now_ist.minute < 30):
        scan_state = "warming up"

    for name, symbol in universe.items():
        try:
            bars = fetch_intraday_bars(symbol, interval="5m")
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            failed.append({"symbol": name, "reason": str(exc)})
            continue
        if not bars.empty:
            cutoff = now_ist - timedelta(minutes=5)
            local_index = (
                bars.index
                if bars.index.tz is not None
                else bars.index.tz_localize(_IST_TZ)
            )
            bars = bars[local_index <= cutoff]
        if bars.empty:
            failed.append({"symbol": name, "reason": "no intraday bars returned"})
            continue
        usable += 1
        res = orb_hit(bars)
        if res is None:
            continue

        last = bars.iloc[-1]
        vol = float(last["volume"]) if pd.notna(last["volume"]) else 0.0
        ref_price = res.get("close") or (res["range_high"] + res["range_low"]) / 2.0
        row_state = "breakout" if res["state"] == "breakout" else "scanned"
        entry = {
            "symbol": name,
            "yahoo_symbol": symbol,
            **res,
            "state": row_state,
            "trade_ready": bool(res.get("trade_ready", False)),
            "signal_state": res.get("signal_state"),
            "volume": vol,
            "price_x_volume": float(ref_price) * vol,
            "date": str(bars.index[-1].date()),
        }

        if row_state == "breakout":
            hits.append(entry)
        else:
            scanned.append(entry)

    hits.sort(key=lambda h: h["price_x_volume"], reverse=True)
    scanned.sort(key=lambda s: s["price_x_volume"], reverse=True)

    if scan_state == "warming up" and (hits or scanned):
        scan_state = "scanned"

    return {
        "scan": "orb",
        "state": scan_state,
        "run_at_ist": now_ist.strftime("%Y-%m-%d %H:%M:%S IST"),
        "fetched": len(universe),
        "usable": usable,
        "failed": failed,
        "hits": hits,
        "scanned": scanned,
        "hit_count": len(hits),
        "scanned_count": len(scanned),
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_watchlist(result: dict[str, Any], path: Path | None = None) -> Path:
    """Write a scan result to data/watchlist.json (default location)."""
    out = Path(path) if path is not None else WATCHLIST_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, default=str)
    return out


def print_summary(result: dict[str, Any]) -> None:
    """Print counts and top hits for a scan result."""
    scan = result["scan"]
    print(f"{scan} scan — {result['run_at_ist']}")
    print(
        f"fetched={result['fetched']} usable={result['usable']} "
        f"hits={result['hit_count']} failed={len(result['failed'])}"
    )
    if result["failed"]:
        print("failed symbols (NOT scanned — coverage is partial):")
        for item in result["failed"][:10]:
            print(f"  {item['symbol']}: {item['reason']}")
        if len(result["failed"]) > 10:
            print(f"  ... and {len(result['failed']) - 10} more")
    if not result["hits"]:
        print("no hits.")
        return
    print("top hits by price x volume:")
    for hit in result["hits"][:10]:
        extra = ""
        if scan == "daily":
            extra = f" setups={','.join(hit['setups'])}"
        else:
            ready_tag = " [TRADE READY]" if hit.get("trade_ready") else ""
            extra = (
                f" {hit['direction']} @ {hit['close']:.2f} "
                f"bar {hit['bar_time']} "
                f"range {hit['range_low']:.2f}-{hit['range_high']:.2f}"
                f"{ready_tag}"
            )
        print(f"  {hit['symbol']}: close {hit['close']:.2f}{extra}")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in ("daily", "orb"):
        print("usage: python -m bhanu.scanner daily|orb")
        return 2
    result = scan_daily() if args[0] == "daily" else scan_orb()
    print_summary(result)
    out = write_watchlist(result)
    print(f"watchlist written to {out}")
    return 0


if __name__ == "__main__":
    # Offline demo: synthetic bars, no network.
    bars = make_synthetic_bars(n=60)
    print("daily setups on synthetic bars:", daily_setups(bars))
    # Synthetic ORB demo: build 5m bars for one morning.
    idx = pd.date_range("2026-11-26 09:15", periods=12, freq="5min", tz=IST)
    closes = [100, 101, 100.5, 102, 103, 104, 103.5, 105, 104, 106, 105, 107]
    orb_bars = pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.4 for c in closes],
            "low": [c - 0.4 for c in closes],
            "close": closes,
            "volume": [1000] * len(closes),
        },
        index=idx,
    )
    print("ORB range (high, low):", orb_range(orb_bars))
    print("ORB hit:", orb_hit(orb_bars))
    if len(sys.argv) > 1:
        raise SystemExit(main())
