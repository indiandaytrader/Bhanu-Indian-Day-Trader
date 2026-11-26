"""Chart data for the dashboard (candlestick / line / flow / sector charts).

Builds JSON-ready payloads from Yahoo 5m bars (via ``bhanu.data``) for
the dashboard pages. No chart library is bundled in Python — pages
render with the TradingView Lightweight Charts library loaded from a
CDN; if the CDN or the data is unavailable the page says "chart
unavailable" instead of faking a chart.

What is built here
------------------
* ``intraday_series`` — one index's intraday candles + close line.
* ``flow_series`` — every symbol's intraday % change line (Smart Money
  Flow: all stocks on one chart, green = up, red = down, NIFTY in the
  middle as the reference line).
* ``sector_map`` — sectors aggregated from the flow data: % change,
  performance vs NIFTY, and today's traded value (bubble size).
* ``orb_chart`` / ``levels_chart`` — candlestick + overlays: VWAP line,
  swing markers, CHoCH/BOS labels, order-block and FVG boxes,
  entry/SL/TP boxes and lines, PDH/PDL lines.
* ``mc_histogram`` — Monte Carlo replay outcomes binned into R buckets.

Honesty: a fetch that fails is reported in the payload's ``error`` /
``failed`` fields and the page shows "chart unavailable" — no fake
series is ever produced. Sectors use the NSE's usual groupings of the
36 stocks in our universe; a sector whose members all failed to fetch
is listed under ``failed``, not shown as flat.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from bhanu import entry as entry_mod
from bhanu import exit as exit_mod
from bhanu import levels as levels_mod
from bhanu import scanner as scanner_mod
from bhanu import structure as structure_mod
from bhanu.data import (
    INDICES,
    STOCKS,
    UNIVERSE,
    DataError,
    fetch_intraday_bars,
)

IST = ZoneInfo("Asia/Kolkata")

#: NSE sector groupings of the 36 stocks in our universe (display names).
SECTORS: dict[str, list[str]] = {
    "Banks": ["HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK"],
    "Financials": ["BAJFINANCE", "BAJAJFINSV"],
    "IT": ["INFY", "TCS", "WIPRO", "HCLTECH", "TECHM"],
    "Auto": ["TATAMOTORS", "MARUTI", "M&M", "EICHERMOT", "HEROMOTOCO"],
    "Metals": ["TATASTEEL", "HINDALCO"],
    "Energy": ["RELIANCE", "ONGC", "NTPC", "POWERGRID", "COALINDIA"],
    "Infra": ["LT", "ULTRACEMCO", "ADANIENT", "ADANIPORTS"],
    "Pharma": ["SUNPHARMA", "DRREDDY", "CIPLA"],
    "FMCG": ["ITC", "HINDUNILVR", "NESTLEIND", "TITAN"],
    "Telecom": ["BHARTIARTL"],
}

#: Colours used by the pages (kept here so tests can assert on them).
COLOR_UP = "#26a69a"
COLOR_DOWN = "#ef5350"
COLOR_INDEX = "#2962ff"
COLOR_VWAP = "#7e57c2"
COLOR_PDH = "#f59e0b"
COLOR_PDL = "#8d6e63"


# ---------------------------------------------------------------------------
# Bar -> chart series
# ---------------------------------------------------------------------------

def _unix(ts) -> int:
    return int(pd.Timestamp(ts).timestamp())


def bars_to_candles(bars: pd.DataFrame) -> list[dict[str, Any]]:
    """OHLC bars -> Lightweight Charts candle data (time = unix seconds)."""
    if bars.empty:
        return []
    out: list[dict[str, Any]] = []
    for ts, row in bars.iterrows():
        if pd.isna(row["close"]):
            continue
        out.append(
            {
                "time": _unix(ts),
                "open": round(float(row["open"]), 2),
                "high": round(float(row["high"]), 2),
                "low": round(float(row["low"]), 2),
                "close": round(float(row["close"]), 2),
            }
        )
    out.sort(key=lambda c: c["time"])
    return out


def bars_to_line(bars: pd.DataFrame) -> list[dict[str, Any]]:
    """OHLC bars -> Lightweight Charts line data (closes)."""
    if bars.empty:
        return []
    out = [
        {"time": _unix(ts), "value": round(float(row["close"]), 2)}
        for ts, row in bars.iterrows()
        if not pd.isna(row["close"])
    ]
    out.sort(key=lambda p: p["time"])
    return out


def bars_to_pct_line(bars: pd.DataFrame) -> list[dict[str, Any]]:
    """OHLC bars -> intraday % change from the first close (Smart Money Flow)."""
    if bars.empty:
        return []
    closes = bars["close"].dropna()
    if closes.empty:
        return []
    first = float(closes.iloc[0])
    if first == 0:
        return []
    return [
        {"time": _unix(ts), "value": round((float(c) / first - 1.0) * 100.0, 3)}
        for ts, c in closes.items()
    ]


def intraday_series(yahoo_symbol: str, days: int = 1) -> dict[str, Any]:
    """Candles + close line for ``yahoo_symbol`` (today by default).

    Returns ``{"symbol", "candles", "line", "error"}`` — ``error`` is a
    string when the fetch failed, and then candles/line are empty.
    """
    try:
        bars = fetch_intraday_bars(yahoo_symbol, interval="5m", days=days)
    except DataError as exc:
        return {"symbol": yahoo_symbol, "candles": [], "line": [], "error": str(exc)}
    if bars.empty:
        return {
            "symbol": yahoo_symbol,
            "candles": [],
            "line": [],
            "error": "no intraday bars returned",
        }
    today = structure_mod.session_bars_today(bars)
    return {
        "symbol": yahoo_symbol,
        "candles": bars_to_candles(today),
        "line": bars_to_line(today),
        "error": None,
    }


# ---------------------------------------------------------------------------
# Smart Money Flow (all symbols' intraday % change on one chart)
# ---------------------------------------------------------------------------

def flow_series(universe: dict[str, str] | None = None) -> dict[str, Any]:
    """Intraday % change lines for every symbol in ``universe``.

    Returns ``{"as_of", "series": [{symbol, line, change_pct, up}],
    "failed": [{symbol, reason}]}``. NIFTY is included as the reference
    line (the index sits in the middle of the flow). A symbol that fails
    to fetch is listed under ``failed`` — never drawn as flat.
    """
    universe = universe or UNIVERSE
    series: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    for name, yahoo_symbol in universe.items():
        try:
            bars = fetch_intraday_bars(yahoo_symbol, interval="5m", days=1)
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            failed.append({"symbol": name, "reason": str(exc)})
            continue
        if bars.empty:
            failed.append({"symbol": name, "reason": "no intraday bars returned"})
            continue
        today = structure_mod.session_bars_today(bars)
        line = bars_to_pct_line(today)
        if not line:
            failed.append({"symbol": name, "reason": "no closes today"})
            continue
        change = line[-1]["value"]
        # Today's traded value (close x volume) — the sector bubble size.
        traded_value = float((today["close"] * today["volume"]).sum())
        series.append(
            {
                "symbol": name,
                "line": line,
                "change_pct": change,
                "up": change >= 0,
                "is_index": name in INDICES,
                "traded_value": traded_value,
            }
        )
    return {
        "as_of": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        "series": series,
        "failed": failed,
    }


def sector_map(flow: dict[str, Any] | None = None) -> dict[str, Any]:
    """Sector bubbles aggregated from ``flow`` (see :func:`flow_series`).

    Per sector: equal-weighted mean intraday % change of its members,
    performance vs NIFTY (``rel_pct``), member count, and today's
    traded value (sum of close x volume over today's 5m bars — the
    bubble size; a real weight proxy, since market caps are not in
    our data). A sector whose members all failed is listed under
    ``failed``. ``nifty_change`` is the reference line's change.
    """
    if flow is None:
        flow = flow_series()
    by_symbol = {s["symbol"]: s for s in flow.get("series", [])}
    nifty = by_symbol.get("NIFTY", {})
    nifty_change = nifty.get("change_pct")
    sectors: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = list(flow.get("failed", []))
    for name, members in SECTORS.items():
        present = [m for m in members if m in by_symbol]
        if not present:
            failed.append({"symbol": name, "reason": "no member data"})
            continue
        changes = [by_symbol[m]["change_pct"] for m in present]
        change = sum(changes) / len(changes)
        traded = sum(by_symbol[m].get("traded_value", 0.0) for m in present)
        sectors.append(
            {
                "name": name,
                "change_pct": round(change, 3),
                "rel_pct": round(change - nifty_change, 3)
                if nifty_change is not None
                else None,
                "stocks": len(present),
                "members": present,
                "traded_value": traded,
                "up": change >= 0,
            }
        )
    sectors.sort(key=lambda s: s["change_pct"], reverse=True)
    return {
        "as_of": flow.get("as_of"),
        "nifty_change": nifty_change,
        "sectors": sectors,
        "failed": failed,
    }


# ---------------------------------------------------------------------------
# Candlestick chart + overlays (ORB / Levels pages)
# ---------------------------------------------------------------------------

def _price_lines(levels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Price-line specs for Lightweight Charts (drops None prices)."""
    return [
        {"price": lv["price"], "label": lv["label"], "color": lv["color"], "style": lv.get("style", 2)}
        for lv in levels
        if lv.get("price") is not None
    ]


def _overlay_payload(
    symbol: str,
    bars: pd.DataFrame,
    extra_lines: list[dict[str, Any]],
    entry: float | None,
    stop: float | None,
    target: float | None,
    signal: str | None,
) -> dict[str, Any]:
    """Candles + VWAP + structure markers + OB/FVG boxes + entry boxes."""
    today = structure_mod.session_bars_today(bars)
    candles = bars_to_candles(today)
    if not candles:
        return {"symbol": symbol, "candles": [], "error": "no bars today"}
    last_time = candles[-1]["time"]

    # VWAP line (session).
    vw = structure_mod.vwap(today)
    vwap_line = [
        {"time": _unix(ts), "value": round(float(v), 2)}
        for ts, v in vw.items()
        if pd.notna(v)
    ]

    # Structure markers: recent swings + CHoCH/BOS breaks.
    markers: list[dict[str, Any]] = []
    for br in structure_mod.structure_breaks(today):
        markers.append(
            {
                "time": br["time"],
                "position": "aboveBar" if br["direction"] == "bearish" else "belowBar",
                "color": COLOR_DOWN if br["direction"] == "bearish" else COLOR_UP,
                "shape": "arrowDown" if br["direction"] == "bearish" else "arrowUp",
                "text": br["kind"],
            }
        )
    for sp in structure_mod.swing_points(today)[-6:]:
        markers.append(
            {
                "time": sp["time"],
                "position": "aboveBar" if sp["kind"] == "high" else "belowBar",
                "color": "#9e9e9e",
                "shape": "circle",
                "text": "SH" if sp["kind"] == "high" else "SL",
            }
        )
    markers.sort(key=lambda m: m["time"])

    # Boxes: order blocks + FVGs + entry/SL/TP boxes.
    boxes: list[dict[str, Any]] = []
    breaks = structure_mod.structure_breaks(today)
    for ob in structure_mod.order_blocks(today, breaks):
        boxes.append(
            {
                "top": ob["top"],
                "bottom": ob["bottom"],
                "from": ob["from_time"],
                "to": None,
                "color": "rgba(38,166,154,0.18)" if ob["kind"] == "bullish" else "rgba(239,83,80,0.18)",
                "border": COLOR_UP if ob["kind"] == "bullish" else COLOR_DOWN,
                "label": ob["label"],
            }
        )
    for fvg in structure_mod.fair_value_gaps(today):
        boxes.append(
            {
                "top": fvg["top"],
                "bottom": fvg["bottom"],
                "from": fvg["from_time"],
                "to": None,
                "color": "rgba(41,98,255,0.12)",
                "border": "#2962ff",
                "label": fvg["label"],
            }
        )
    if entry is not None and stop is not None and target is not None and signal:
        trigger_time = None
        rng = scanner_mod.orb_range(today)
        if rng is not None:
            rh, rl = rng
            for ts, row in today.iterrows():
                c = float(row["close"])
                if (signal == "long" and c > rh) or (signal == "short" and c < rl):
                    trigger_time = _unix(ts)
                    break
        box_from = trigger_time or candles[0]["time"]
        boxes.append(
            {
                "top": max(entry, target),
                "bottom": min(entry, target),
                "from": box_from,
                "to": None,
                "color": "rgba(38,166,154,0.15)",
                "border": COLOR_UP,
                "label": "TARGET",
            }
        )
        boxes.append(
            {
                "top": max(entry, stop),
                "bottom": min(entry, stop),
                "from": box_from,
                "to": None,
                "color": "rgba(239,83,80,0.15)",
                "border": COLOR_DOWN,
                "label": "STOP",
            }
        )

    lines = _price_lines(extra_lines)
    if entry is not None:
        lines.append({"price": entry, "label": "ENTRY", "color": COLOR_INDEX, "style": 0})
    if stop is not None:
        lines.append({"price": stop, "label": "SL", "color": COLOR_DOWN, "style": 0})
    if target is not None:
        lines.append({"price": target, "label": "TP", "color": COLOR_UP, "style": 0})

    return {
        "symbol": symbol,
        "candles": candles,
        "vwap": vwap_line,
        "lines": lines,
        "markers": markers,
        "boxes": boxes,
        "last_time": last_time,
        "error": None,
    }


def orb_chart(yahoo_symbol: str, symbol_label: str | None = None) -> dict[str, Any]:
    """Candlestick + overlays for one symbol's ORB setup.

    ORB range lines, entry/SL/TP (from the ORB plan), PDH/PDL, VWAP,
    structure markers and OB/FVG boxes. Returns an ``error`` payload
    when there is no ORB setup or no data — never a fake chart.
    """
    label = symbol_label or yahoo_symbol
    try:
        bars = fetch_intraday_bars(yahoo_symbol, interval="5m", days=1)
    except DataError as exc:
        return {"symbol": label, "candles": [], "error": str(exc)}
    if bars.empty:
        return {"symbol": label, "candles": [], "error": "no intraday bars returned"}
    today = structure_mod.session_bars_today(bars)
    hit = scanner_mod.orb_hit(today)
    if hit is None:
        payload = _overlay_payload(label, bars, [], None, None, None, None)
        payload["error"] = "no ORB trigger yet today — range only"
        return payload
    plan = entry_mod.plan_orb_entry(today, direction=hit["direction"], symbol=label)
    entry = stop = target = None
    if plan is not None:
        entry, stop, target = plan.entry, plan.stop, plan.target
    # PDH/PDL for the lines.
    pdh = pdl = None
    try:
        daily = levels_mod.fetch_daily_bars(yahoo_symbol, days=30)
        pdh_pdl = levels_mod.prev_day_high_low(daily)
        if pdh_pdl is not None:
            pdh, pdl = pdh_pdl
    except Exception:  # noqa: BLE001 - PDH/PDL lines just omitted
        pass
    extra = [
        {"price": hit["range_high"], "label": "ORB HIGH", "color": COLOR_UP, "style": 2},
        {"price": hit["range_low"], "label": "ORB LOW", "color": COLOR_DOWN, "style": 2},
        {"price": pdh, "label": "PDH", "color": COLOR_PDH, "style": 2},
        {"price": pdl, "label": "PDL", "color": COLOR_PDL, "style": 2},
    ]
    payload = _overlay_payload(label, bars, extra, entry, stop, target, hit["direction"])
    payload["signal"] = hit["direction"]
    payload["range_high"] = hit["range_high"]
    payload["range_low"] = hit["range_low"]
    payload["entry"] = entry
    payload["stop"] = stop
    payload["target"] = target
    return payload


def levels_chart(yahoo_symbol: str, symbol_label: str | None = None) -> dict[str, Any]:
    """Candlestick + overlays for one index's levels (S/R, PDH/PDL, ORB)."""
    label = symbol_label or yahoo_symbol
    try:
        row = levels_mod.compute_levels(label, yahoo_symbol)
    except Exception as exc:  # noqa: BLE001 - report, don't fake
        return {"symbol": label, "candles": [], "error": str(exc)}
    try:
        bars = fetch_intraday_bars(yahoo_symbol, interval="5m", days=1)
    except DataError as exc:
        return {"symbol": label, "candles": [], "error": str(exc)}
    extra = [
        {"price": row.get("nearest_support"), "label": "SUPPORT", "color": COLOR_UP, "style": 0},
        {"price": row.get("nearest_resistance"), "label": "RESIST", "color": COLOR_DOWN, "style": 0},
        {"price": row.get("pdh"), "label": "PDH", "color": COLOR_PDH, "style": 2},
        {"price": row.get("pdl"), "label": "PDL", "color": COLOR_PDL, "style": 2},
        {"price": row.get("orb_high"), "label": "ORB HIGH", "color": COLOR_UP, "style": 2},
        {"price": row.get("orb_low"), "label": "ORB LOW", "color": COLOR_DOWN, "style": 2},
    ]
    payload = _overlay_payload(
        label,
        bars,
        extra,
        row.get("entry"),
        row.get("stop"),
        row.get("target"),
        row.get("signal"),
    )
    payload["levels"] = row
    return payload


# ---------------------------------------------------------------------------
# Monte Carlo distribution (R histogram)
# ---------------------------------------------------------------------------

#: R buckets for the Monte Carlo distribution chart.
MC_BUCKETS: list[tuple[str, float, float]] = [
    ("Stop (-1R)", -1.0, -1.0),
    ("-1R to 0R", -1.0, 0.0),
    ("0R to +1R", 0.0, 1.0),
    ("+1R to +2R", 1.0, 2.0),
    ("Target (+2R)", 2.0, 2.0),
]


def mc_histogram(outcomes: list[float], avg_r: float | None = None) -> dict[str, Any]:
    """Bin Monte Carlo replay R outcomes into buckets for the chart.

    The two exact outcomes (stop hit = -1R, target hit = +2R) get their
    own buckets; partial "neither" outcomes fall into the range buckets
    between them. Returns ``{"buckets": [{label, count, pct}], "n",
    "avg_r"}`` — empty outcomes give an empty-buckets result, never
    invented counts.
    """
    counts = [0] * len(MC_BUCKETS)
    for r in outcomes:
        # Epsilon: the target outcome is reward/risk computed in floats
        # (e.g. 139.66/69.83), which can land a hair under 2.0.
        if r <= -1.0 + 1e-9:
            counts[0] += 1
        elif r < 0.0:
            counts[1] += 1
        elif r < 1.0:
            counts[2] += 1
        elif r < 2.0 - 1e-9:
            counts[3] += 1
        else:
            counts[4] += 1
    n = len(outcomes)
    buckets = [
        {
            "label": label,
            "count": c,
            "pct": round(100.0 * c / n, 1) if n else 0.0,
        }
        for (label, _, _), c in zip(MC_BUCKETS, counts)
    ]
    return {"buckets": buckets, "n": n, "avg_r": avg_r}


def main() -> int:
    from bhanu.data import make_synthetic_bars

    bars = make_synthetic_bars(n=60)
    print(f"candles: {len(bars_to_candles(bars))}, line pts: {len(bars_to_line(bars))}")
    print(f"pct line pts: {len(bars_to_pct_line(bars))}")
    print(f"sectors: {len(SECTORS)} groups, {sum(len(v) for v in SECTORS.values())} stocks")
    print(f"MC histogram: {mc_histogram([-1.0, -0.5, 0.2, 1.5, 2.0, 2.0], 0.2)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
