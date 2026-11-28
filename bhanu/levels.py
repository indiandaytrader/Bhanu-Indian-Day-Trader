"""Live support / resistance levels + entry/exit display for NSE indices.

What it shows (per index: NIFTY, BANKNIFTY, SENSEX, or any universe symbol)
---------------------------------------------------------------------------
* Current price (last 5m close).
* Nearest support / nearest resistance — the closest level below / above
  the price, drawn from: recent swing highs/lows on 5m bars (last 5
  sessions), previous-day high/low (PDH/PDL), and today's opening-range
  high/low (09:15-09:30 IST).
* Enriched per-level metrics: ``strength`` (1-100), ``touches`` count,
  ``distance_pts`` / ``distance_pct`` from current price, ``kind``
  (``PDH`` / ``PDL`` / ``ORB`` / ``swing``), ``side`` (``support`` /
  ``resistance``), and ``zone_low`` / ``zone_high`` for shaded zones.
* Top ``summary`` dict: ``trend``, ``support``, ``resistance``,
  ``distance``, ``setup``, ``risk_reward``.
* PDH / PDL, ORB high / ORB low.
* The ORB signal for today (from ``bhanu.scanner``) with entry, stop
  (range midpoint, with the 1.5x ATR stop alongside) and targets
  TP1/TP2/TP3 (1R/2R/3R; ``target`` is TP2) from ``bhanu.entry`` /
  ``bhanu.exit`` — so a trader sees at a glance "enter here, exit
  here, support here, resistance here".

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
# Level enrichment: strength (1-100), touches, distance, shaded zones, summary
# ---------------------------------------------------------------------------

def _classify_kind(
    level_price: float,
    price: float,
    pdh: float | None,
    pdl: float | None,
    orb_high: float | None,
    orb_low: float | None,
) -> str:
    """Tag a clustered level with its highest-priority kind: PDH/PDL/ORB/swing."""
    tol = max(abs(price) * CLUSTER_TOL_PCT / 100.0, 1e-6)
    if pdh is not None and abs(level_price - pdh) <= tol:
        return "PDH"
    if pdl is not None and abs(level_price - pdl) <= tol:
        return "PDL"
    if orb_high is not None and abs(level_price - orb_high) <= tol:
        return "ORB"
    if orb_low is not None and abs(level_price - orb_low) <= tol:
        return "ORB"
    return "swing"


def score_level(
    level_price: float,
    price: float,
    bars_5m: pd.DataFrame,
    kind: str,
    raw_levels: list[float],
    atr: float | None = None,
) -> dict[str, Any]:
    """Compute strength (1-100), touches, distance, and shaded zone for one level.

    Strength formula (capped 1-100):
        strength = clamp(1, 100, round(touch_score + recency_score + vol_score + confluence_score))

    where:
    * ``touch_score`` (0-45): ``min(45, touches * 12)``, where ``touches`` is
      the number of 5m bars in ``bars_5m`` whose ``[low, high]`` range trades
      within ``CLUSTER_TOL_PCT`` (0.05%) of ``level_price`` (min 1).
    * ``recency_score`` (0-25): ``25 * (last_touch_idx / max(1, n - 1))`` —
      levels tested more recently score higher.
    * ``vol_score`` (0-15): ``min(15, 7.5 * (avg_touch_vol / mean_vol))``
      when positive volume data exists on the 5m bars, else 0.
    * ``confluence_score`` (0-15): base weight by kind (PDH/PDL = 12,
      ORB = 10, swing = 6) plus ``+3`` if two or more raw levels clustered
      into this zone (capped at 15).
    """
    tol = max(abs(price) * CLUSTER_TOL_PCT / 100.0, 1e-6)
    n = len(bars_5m)
    touches = 0
    last_touch_idx = 0
    touch_vols: list[float] = []
    has_vol = "volume" in bars_5m.columns

    if n > 0:
        highs = bars_5m["high"].to_numpy(dtype=float)
        lows = bars_5m["low"].to_numpy(dtype=float)
        vols = bars_5m["volume"].to_numpy(dtype=float) if has_vol else None
        for i in range(n):
            if lows[i] - tol <= level_price <= highs[i] + tol:
                touches += 1
                last_touch_idx = i
                if vols is not None and pd.notna(vols[i]) and vols[i] > 0:
                    touch_vols.append(float(vols[i]))

    touches_out = max(1, touches)
    touch_score = min(45.0, touches_out * 12.0)
    recency_score = (25.0 * last_touch_idx / max(1, n - 1)) if touches > 0 and n > 1 else 5.0

    vol_score = 0.0
    if has_vol and touch_vols:
        pos_vols = bars_5m["volume"].dropna()
        pos_vols = pos_vols[pos_vols > 0]
        if not pos_vols.empty:
            mean_vol = float(pos_vols.mean())
            if mean_vol > 0:
                avg_touch_vol = sum(touch_vols) / len(touch_vols)
                vol_score = min(15.0, 7.5 * (avg_touch_vol / mean_vol))

    kind_base = {"PDH": 12.0, "PDL": 12.0, "ORB": 10.0, "swing": 6.0}.get(kind, 6.0)
    cluster_count = sum(1 for lv in raw_levels if abs(lv - level_price) <= tol)
    confluence_score = min(15.0, kind_base + (3.0 if cluster_count >= 2 else 0.0))

    strength = max(1, min(100, int(round(touch_score + recency_score + vol_score + confluence_score))))

    dist_pts = round(level_price - price, 2)
    dist_pct = round((dist_pts / price) * 100.0, 2) if price else 0.0
    side = "support" if level_price < price else "resistance"

    zone_half = max(abs(price) * 0.0008, (float(atr) * 0.25) if atr else 0.0)
    zone_low = round(level_price - zone_half, 2)
    zone_high = round(level_price + zone_half, 2)

    return {
        "price": round(float(level_price), 2),
        "kind": kind,
        "side": side,
        "strength": strength,
        "touches": touches_out,
        "distance_pts": dist_pts,
        "distance_pct": dist_pct,
        "zone_low": zone_low,
        "zone_high": zone_high,
    }


def _build_summary(
    price: float,
    today_bars: pd.DataFrame,
    support: float | None,
    resistance: float | None,
    signal: str | None,
    entry: float | None,
    stop: float | None,
    target: float | None,
) -> dict[str, Any]:
    """Top summary dict: trend, support, resistance, distance, setup, risk_reward."""
    trend = "Range"
    if not today_bars.empty:
        day_open = float(today_bars["open"].iloc[0])
        if day_open > 0:
            chg_pct = (price - day_open) / day_open * 100.0
            if chg_pct >= 0.20:
                trend = "Bullish"
            elif chg_pct <= -0.20:
                trend = "Bearish"

    sup_pts = round(price - support, 2) if support is not None else None
    sup_pct = round((sup_pts / price) * 100.0, 2) if (sup_pts is not None and price) else None
    res_pts = round(resistance - price, 2) if resistance is not None else None
    res_pct = round((res_pts / price) * 100.0, 2) if (res_pts is not None and price) else None

    if signal == "long":
        setup_label = "CALL"
    elif signal == "short":
        setup_label = "PUT"
    else:
        setup_label = "Watching"

    rr_label = "—"
    if entry is not None and stop is not None and target is not None:
        risk = abs(float(entry) - float(stop))
        reward = abs(float(target) - float(entry))
        if risk > 0:
            rr_label = f"1:{reward / risk:.1f}"

    return {
        "trend": trend,
        "support": round(support, 2) if support is not None else None,
        "resistance": round(resistance, 2) if resistance is not None else None,
        "distance": {
            "support_pts": sup_pts,
            "support_pct": sup_pct,
            "resistance_pts": res_pts,
            "resistance_pct": res_pct,
        },
        "setup": setup_label,
        "risk_reward": rr_label,
    }


# ---------------------------------------------------------------------------
# Per-symbol levels + ORB entry/exit
# ---------------------------------------------------------------------------

def compute_levels(
    symbol: str,
    yahoo_symbol: str | None = None,
    lookback_sessions: int = LOOKBACK_SESSIONS,
) -> dict[str, Any]:
    """Live levels + ORB entry/exit for one index or stock.

    Fetches 5m bars (``lookback_sessions`` sessions) and daily bars,
    then returns a dict with all existing fields plus enriched
    ``strength``, ``touches``, ``distance_pts``, ``distance_pct``,
    ``level_details`` (per-level dicts with kind/side/strength/touches/
    distance/zone_low/zone_high), and ``summary`` (trend, support,
    resistance, distance, setup, risk_reward). Raises :class:`DataError`
    when the fetch fails — never returns fabricated levels.
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

    # Enrich every clustered level with strength (1-100), touches, distance, kind & zone.
    level_details: list[dict[str, Any]] = []
    for lv in levels:
        kind = _classify_kind(lv, price, pdh, pdl, orb_high, orb_low)
        level_details.append(score_level(lv, price, bars_5m, kind, raw_levels, atr=atr))

    sup_detail = next((d for d in level_details if support is not None and abs(d["price"] - support) < 0.02), None)
    res_detail = next((d for d in level_details if resistance is not None and abs(d["price"] - resistance) < 0.02), None)

    # ORB signal + entry/exit (reuse the existing entry/exit rules).
    signal: str | None = None
    entry = stop = atr_stop = target = None
    tp1 = tp2 = tp3 = None
    hit = scanner_mod.orb_hit(today_bars)
    if hit is not None and hit.get("direction") is not None:
        signal = hit["direction"]
        plan = entry_mod.plan_orb_entry(
            today_bars, direction=signal, symbol=symbol
        )
        if plan is not None:
            entry = plan.entry
            stop = plan.stop
            target = plan.target
            tp1 = plan.tp1
            tp2 = plan.tp2
            tp3 = plan.tp3
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

    # Primary level metrics directly on the row dict (for table & summary access).
    primary = sup_detail if (signal == "long" and sup_detail) else (
        res_detail if (signal == "short" and res_detail) else (sup_detail or res_detail)
    )
    summary = _build_summary(
        price, today_bars, support, resistance, signal, entry, stop, target
    )

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
        "level_details": level_details,
        "strength": primary["strength"] if primary else None,
        "touches": primary["touches"] if primary else None,
        "distance_pts": primary["distance_pts"] if primary else None,
        "distance_pct": primary["distance_pct"] if primary else None,
        "support_strength": sup_detail["strength"] if sup_detail else None,
        "support_touches": sup_detail["touches"] if sup_detail else None,
        "support_distance_pts": sup_detail["distance_pts"] if sup_detail else None,
        "support_distance_pct": sup_detail["distance_pct"] if sup_detail else None,
        "resistance_strength": res_detail["strength"] if res_detail else None,
        "resistance_touches": res_detail["touches"] if res_detail else None,
        "resistance_distance_pts": res_detail["distance_pts"] if res_detail else None,
        "resistance_distance_pct": res_detail["distance_pct"] if res_detail else None,
        "summary": summary,
        "atr": atr,
        "signal": signal,
        "entry": entry,
        "stop": stop,
        "atr_stop": atr_stop,
        "target": target,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
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
            f"support {sup:,.2f} (str {row.get('support_strength')}) | "
            f"resistance {res:,.2f} (str {row.get('resistance_strength')}) | "
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
