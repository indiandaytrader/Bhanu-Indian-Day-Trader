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
    fetch_intraday_bars_meta,
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

#: Symbol -> sector (for the Smart Money spaghetti's left labels).
_SYMBOL_SECTOR: dict[str, str] = {
    sym: name for name, members in SECTORS.items() for sym in members
}

#: Colours used by the pages (kept here so tests can assert on them).
#: Reference palette: green #2f9e5f, red #d6455d, blue #2563eb.
COLOR_UP = "#2f9e5f"
COLOR_DOWN = "#d6455d"
COLOR_INDEX = "#2563eb"
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

    Returns ``{"symbol", "candles", "line", "error", "stale", "as_of"}``
    — ``error`` is a string when the fetch failed, and then
    candles/line are empty. ``stale`` is True when the bars came from
    an expired disk cache because the live fetch failed (real data,
    just older); ``as_of`` is that cache's save time.
    """
    try:
        bars, stale, as_of = fetch_intraday_bars_meta(
            yahoo_symbol, interval="5m", days=days)
    except DataError as exc:
        return {"symbol": yahoo_symbol, "candles": [], "line": [],
                "error": str(exc), "stale": False, "as_of": ""}
    if bars.empty:
        return {
            "symbol": yahoo_symbol,
            "candles": [],
            "line": [],
            "error": "no intraday bars returned",
            "stale": False,
            "as_of": "",
        }
    today = structure_mod.session_bars_today(bars)
    return {
        "symbol": yahoo_symbol,
        "candles": bars_to_candles(today),
        "line": bars_to_line(today),
        "error": None,
        "stale": stale,
        "as_of": as_of,
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
                "sector": _SYMBOL_SECTOR.get(name, "Index"),
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
        # Top member (strongest today) — the bubble links to its detail page.
        top_member = max(present, key=lambda m: by_symbol[m]["change_pct"])
        sectors.append(
            {
                "name": name,
                "change_pct": round(change, 3),
                "rel_pct": round(change - nifty_change, 3)
                if nifty_change is not None
                else None,
                "stocks": len(present),
                "members": present,
                "top_member": top_member,
                "top_change": by_symbol[top_member]["change_pct"],
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

#: Label priority for price lines (1 = most important). When two lines
#: sit so close that their axis labels would overlap, the lower
#: priority line keeps its line but loses its label.
_LINE_PRIORITY: dict[str, int] = {
    "ENTRY": 1, "SL": 2, "TP": 3, "PDH": 4, "PDL": 5,
    "ORB HIGH": 6, "ORB LOW": 7, "SUPPORT": 8, "RESIST": 9,
}


def _price_lines(
    levels: list[dict[str, Any]], price_range: float | None = None
) -> list[dict[str, Any]]:
    """Price-line specs for Lightweight Charts (drops None prices).

    Each spec carries ``group`` ("entry" for ENTRY/SL/TP, else
    "levels") and ``label_visible`` (default True). Axis-label
    collisions are resolved afterwards by ``_dedupe_line_labels``
    over the full line set (entry/SL/TP lines are appended later).
    ``price_range`` is accepted for callers that dedupe here and is
    otherwise unused.
    """
    lines = [
        {
            "price": lv["price"],
            "label": lv["label"],
            "color": lv["color"],
            "style": lv.get("style", 2),
            "group": "entry" if lv["label"] in ("ENTRY", "SL", "TP") else "levels",
            "label_visible": True,
        }
        for lv in levels
        if lv.get("price") is not None
    ]
    return lines


def _overlay_payload(
    symbol: str,
    bars: pd.DataFrame,
    extra_lines: list[dict[str, Any]],
    entry: float | None,
    stop: float | None,
    target: float | None,
    signal: str | None,
    chart_bars: pd.DataFrame | None = None,
    struct_bars: pd.DataFrame | None = None,
    vwap: bool = True,
) -> dict[str, Any]:
    """Candles + VWAP + structure markers + OB/FVG boxes + entry boxes.

    ``chart_bars`` are the candles drawn (default: today's session);
    ``struct_bars`` are the bars the structure is computed on
    (default: same as ``chart_bars``); ``vwap=False`` skips the VWAP
    line (daily charts — a session indicator makes no sense there).
    Boxes carry a ``kind`` (``ob`` / ``fvg`` / ``target`` / ``stop``)
    and markers a ``group`` (``structure``) so the page can toggle
    each overlay on and off.
    """
    chart_bars = structure_mod.session_bars_today(bars) if chart_bars is None else chart_bars
    struct_bars = chart_bars if struct_bars is None else struct_bars
    candles = bars_to_candles(chart_bars)
    if not candles:
        return {"symbol": symbol, "candles": [], "error": "no bars today"}
    last_time = candles[-1]["time"]

    # VWAP line (session).
    vwap_line: list[dict[str, Any]] = []
    if vwap:
        vw = structure_mod.vwap(struct_bars)
        vwap_line = [
            {"time": _unix(ts), "value": round(float(v), 2)}
            for ts, v in vw.items()
            if pd.notna(v)
        ]

    # Structure markers: recent swings + CHoCH/BOS breaks.
    markers: list[dict[str, Any]] = []
    for br in structure_mod.structure_breaks(struct_bars):
        markers.append(
            {
                "time": br["time"],
                "position": "aboveBar" if br["direction"] == "bearish" else "belowBar",
                "color": COLOR_DOWN if br["direction"] == "bearish" else COLOR_UP,
                "shape": "arrowDown" if br["direction"] == "bearish" else "arrowUp",
                "text": br["kind"],
                "group": "structure",
            }
        )
    for sp in structure_mod.swing_points(struct_bars)[-6:]:
        markers.append(
            {
                "time": sp["time"],
                "position": "aboveBar" if sp["kind"] == "high" else "belowBar",
                "color": "#9e9e9e",
                "shape": "circle",
                "text": "SH" if sp["kind"] == "high" else "SL",
                "group": "structure",
            }
        )
    markers.sort(key=lambda m: m["time"])

    # Boxes: order blocks + FVGs + entry/SL/TP boxes.
    boxes: list[dict[str, Any]] = []
    breaks = structure_mod.structure_breaks(struct_bars)
    for ob in structure_mod.order_blocks(struct_bars, breaks):
        boxes.append(
            {
                "top": ob["top"],
                "bottom": ob["bottom"],
                "from": ob["from_time"],
                "to": None,
                "color": "rgba(38,166,154,0.18)" if ob["kind"] == "bullish" else "rgba(239,83,80,0.18)",
                "border": COLOR_UP if ob["kind"] == "bullish" else COLOR_DOWN,
                "label": ob["label"],
                "kind": "ob",
            }
        )
    for fvg in structure_mod.fair_value_gaps(struct_bars):
        boxes.append(
            {
                "top": fvg["top"],
                "bottom": fvg["bottom"],
                "from": fvg["from_time"],
                "to": None,
                "color": "rgba(37,99,235,0.12)",
                "border": COLOR_INDEX,
                "label": fvg["label"],
                "kind": "fvg",
            }
        )
    if entry is not None and stop is not None and target is not None and signal:
        trigger_time = None
        rng = scanner_mod.orb_range(struct_bars)
        if rng is not None:
            rh, rl = rng
            for ts, row in struct_bars.iterrows():
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
                "kind": "target",
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
                "kind": "stop",
            }
        )

    price_range = None
    if not chart_bars.empty:
        price_range = float(chart_bars["high"].max() - chart_bars["low"].min())
    lines = _price_lines(extra_lines, price_range)
    if entry is not None:
        lines.append({"price": entry, "label": "ENTRY", "color": COLOR_INDEX, "style": 0, "group": "entry", "label_visible": True})
    if stop is not None:
        lines.append({"price": stop, "label": "SL", "color": COLOR_DOWN, "style": 0, "group": "entry", "label_visible": True})
    if target is not None:
        lines.append({"price": target, "label": "TP", "color": COLOR_UP, "style": 0, "group": "entry", "label_visible": True})
    # The entry/SL/TP lines are appended after the dedupe above, so
    # re-run the collision check over the full set: an ENTRY line a
    # few points from PDL must not print on top of it.
    if price_range and price_range > 0:
        lines = _dedupe_line_labels(lines, price_range)

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


def _dedupe_line_labels(
    lines: list[dict[str, Any]], price_range: float
) -> list[dict[str, Any]]:
    """Hide the axis label of the lower-priority line in each cluster
    of lines closer than 4% of ``price_range`` (see ``_price_lines``).
    The lines themselves are always drawn."""
    if len(lines) <= 1 or not price_range or price_range <= 0:
        return lines
    threshold = 0.04 * price_range
    ordered = sorted(lines, key=lambda ln: ln["price"])

    def _resolve(cluster: list[dict[str, Any]]) -> None:
        if len(cluster) <= 1:
            return
        best = min(cluster, key=lambda ln: _LINE_PRIORITY.get(ln["label"], 10))
        for ln in cluster:
            ln["label_visible"] = ln is best

    cluster: list[dict[str, Any]] = []
    prev: dict[str, Any] | None = None
    for ln in ordered:
        if prev is not None and abs(ln["price"] - prev["price"]) <= threshold:
            cluster.append(ln)
        else:
            _resolve(cluster)
            cluster = [ln]
        prev = ln
    _resolve(cluster)
    return lines


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


#: Detail-page intervals: Yahoo interval + how many days to fetch.
_STOCK_INTERVALS: dict[str, tuple[str, int]] = {
    "1m": ("1m", 1),
    "5m": ("5m", 1),
    "15m": ("15m", 1),
    "1h": ("1h", 5),
}


def stock_chart(
    yahoo_symbol: str,
    symbol_label: str | None = None,
    interval: str = "5m",
    row: dict[str, Any] | None = None,
    past_trades: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Candlestick + overlays for the /stock detail page.

    ``interval`` is one of ``1m`` / ``5m`` / ``15m`` / ``1h`` / ``D``.
    Intraday intervals use today's session bars (VWAP, order blocks,
    FVG, structure markers all computed on them); ``D`` uses the last
    180 daily bars with structure on the last 60 (no VWAP — a session
    indicator makes no sense on daily bars) plus the symbol's past
    ORB trades as markers (``past_trades`` from
    ``bhanu.stock.orb_history``, matched to daily candles by date).
    Entry/SL/TP + PDH/PDL + ORB lines come from ``row`` (a
    ``levels.compute_levels`` result; computed here when omitted).
    Returns ``"overlays"`` availability flags — a group with nothing
    to show (no order blocks, no past trades, VWAP on D, liquidity
    always) reports False so the page pill says "unavailable" instead
    of faking the overlay.
    """
    label = symbol_label or yahoo_symbol
    iv = (interval or "5m").strip()
    if iv == "1H":
        iv = "1h"
    if iv not in ("1m", "5m", "15m", "1h", "D"):
        return {"symbol": label, "candles": [], "error": f"unknown interval {interval}"}
    if row is None:
        try:
            row = levels_mod.compute_levels(label, yahoo_symbol)
        except Exception:  # noqa: BLE001 - chart still draws, setup lines omitted
            row = None
    row = row or {}
    entry = row.get("entry")
    stop = row.get("stop")
    target = row.get("target")
    signal = row.get("signal")
    extra = [
        {"price": row.get("nearest_support"), "label": "SUPPORT", "color": COLOR_UP, "style": 0},
        {"price": row.get("nearest_resistance"), "label": "RESIST", "color": COLOR_DOWN, "style": 0},
        {"price": row.get("pdh"), "label": "PDH", "color": COLOR_PDH, "style": 2},
        {"price": row.get("pdl"), "label": "PDL", "color": COLOR_PDL, "style": 2},
        {"price": row.get("orb_high"), "label": "ORB HIGH", "color": COLOR_UP, "style": 2},
        {"price": row.get("orb_low"), "label": "ORB LOW", "color": COLOR_DOWN, "style": 2},
    ]
    if iv == "D":
        try:
            from bhanu.data import fetch_daily_bars

            daily = fetch_daily_bars(yahoo_symbol, days=180)
        except DataError as exc:
            return {"symbol": label, "candles": [], "error": str(exc)}
        if daily.empty:
            return {"symbol": label, "candles": [], "error": "no daily bars returned"}
        struct = daily.tail(60)
        payload = _overlay_payload(
            label, daily, extra, entry, stop, target, signal,
            chart_bars=daily, struct_bars=struct, vwap=False,
        )
        # Past ORB trades as markers, matched to daily candles by date.
        if past_trades:
            from datetime import date as _date

            date_to_time = {ts.date(): _unix(ts) for ts in daily.index}
            for tr in past_trades[-20:]:
                try:
                    d = _date.fromisoformat(str(tr.get("date", "")))
                except ValueError:
                    continue
                t = date_to_time.get(d)
                if t is None:
                    continue
                won = tr.get("result") == "win"
                lost = tr.get("result") == "loss"
                payload["markers"].append(
                    {
                        "time": t,
                        "position": "aboveBar" if tr.get("direction") == "long" else "belowBar",
                        "color": COLOR_UP if won else (COLOR_DOWN if lost else "#9e9e9e"),
                        "shape": "arrowUp" if tr.get("direction") == "long" else "arrowDown",
                        "text": (
                            f"{'LONG' if tr.get('direction') == 'long' else 'SHORT'} "
                            f"{float(tr.get('outcome_r', 0.0)):+.2f}R"
                        ),
                        "group": "trades",
                    }
                )
            payload["markers"].sort(key=lambda m: m["time"])
    else:
        yahoo_interval, days = _STOCK_INTERVALS[iv]
        try:
            bars = fetch_intraday_bars(yahoo_symbol, interval=yahoo_interval, days=days)
        except DataError as exc:
            return {"symbol": label, "candles": [], "error": str(exc)}
        if bars.empty:
            return {"symbol": label, "candles": [], "error": "no intraday bars returned"}
        payload = _overlay_payload(label, bars, extra, entry, stop, target, signal)
    payload["interval"] = iv
    payload["levels"] = row
    boxes = payload.get("boxes", [])
    markers = payload.get("markers", [])
    payload["overlays"] = {
        "orderblock": any(b.get("kind") == "ob" for b in boxes),
        "fvg": any(b.get("kind") == "fvg" for b in boxes),
        "structure": any(m.get("group") == "structure" for m in markers),
        "liquidity": False,
        "vwap": bool(payload.get("vwap")),
        "trades": any(m.get("group") == "trades" for m in markers),
    }
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
