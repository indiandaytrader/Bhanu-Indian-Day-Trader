"""tests/test_remaining_fixes.py — regression tests for the 27 Nov 2026
remaining-issues round (Gemini version).

1. Indicators: RSI (14) + MACD (12,26,9) computed server-side and
   carried on every candle-chart payload (Levels / ORB / stock detail),
   with BUY/SELL markers on the trigger bar and overlay pills on the
   Levels/ORB pages (the stock page already had pills).
2. ORB chart at 0 hits: the range-only chart renders (candles + ORB
   range / PDH / PDL lines) with a ``note`` — never an error payload —
   and the ORB page defaults to NIFTY so the chart loads at all.
3. Sector map: bubble labels no longer print on top of each other
   (collision-aware placement).
4. Smart Money flow: refresh cadence 300s -> 120s (poller + API cache).
5. Chart load speed: /api/stock takes the quote from the poller spark
   (indices) or a 60s-cached Yahoo quote instead of a fresh fetch
   every load; the detail poller interval is 500ms, not 1200ms.

Offline: all network access is mocked.
"""
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from bhanu import charts as charts_mod
from bhanu import data as data_mod
from bhanu import levels as levels_mod
from bhanu import poller as poller_mod
from web import app

IST = ZoneInfo("Asia/Kolkata")


def _bars_30():
    """30 synthetic NIFTY 5m bars, 09:15-11:40 IST, with an ORB long
    trigger: range high 25718 (first 3 bars), close above it at 09:30."""
    base = datetime(2026, 11, 26, 9, 15, tzinfo=IST)
    closes = [25710, 25705, 25708, 25725, 25730, 25728, 25735, 25740,
              25738, 25745, 25750, 25748, 25755, 25760, 25758, 25765,
              25770, 25768, 25775, 25780, 25778, 25785, 25790, 25788,
              25795, 25800, 25798, 25805, 25810, 25808]
    idx = pd.DatetimeIndex(
        [base + pd.Timedelta(minutes=5 * i) for i in range(30)], tz=IST)
    return pd.DataFrame(
        {
            "open": [c - 2 for c in closes],
            "high": [c + 5 for c in closes],
            "low": [c - 5 for c in closes],
            "close": closes,
            "volume": [1000 + 10 * i for i in range(30)],
        },
        index=idx,
    )


def _bars_30_short():
    """30 bars with an ORB short trigger: range low 25700 (first 3
    bars), close below it at 09:30, then a downtrend."""
    base = datetime(2026, 11, 26, 9, 15, tzinfo=IST)
    closes = [25710, 25705, 25708, 25680, 25675, 25678, 25670, 25665,
              25668, 25660, 25655, 25658, 25650, 25645, 25648, 25640,
              25635, 25638, 25630, 25625, 25628, 25620, 25615, 25618,
              25610, 25605, 25608, 25600, 25595, 25598]
    idx = pd.DatetimeIndex(
        [base + pd.Timedelta(minutes=5 * i) for i in range(30)], tz=IST)
    return pd.DataFrame(
        {
            "open": [c + 2 for c in closes],
            "high": [c + 5 for c in closes],
            "low": [c - 5 for c in closes],
            "close": closes,
            "volume": [1000 + 10 * i for i in range(30)],
        },
        index=idx,
    )


def _bars_flat():
    """6 bars that never close outside the 09:15-09:30 range (no ORB hit)."""
    base = datetime(2026, 11, 26, 9, 15, tzinfo=IST)
    closes = [25710, 25705, 25708, 25712, 25710, 25709]
    idx = pd.DatetimeIndex(
        [base + pd.Timedelta(minutes=5 * i) for i in range(6)], tz=IST)
    return pd.DataFrame(
        {
            "open": [c - 2 for c in closes],
            "high": [c + 8 for c in closes],
            "low": [c - 8 for c in closes],
            "close": closes,
            "volume": [1000] * 6,
        },
        index=idx,
    )


def _levels_row(signal="long"):
    return {
        "symbol": "NIFTY", "price": 25808.0,
        "nearest_support": 25650.0, "nearest_resistance": 25800.0,
        "pdh": 25800.0, "pdl": 25550.0,
        "orb_high": 25718.0, "orb_low": 25687.0,
        "signal": signal, "entry": 25725.0, "stop": 25702.5,
        "atr_stop": 25690.0, "target": 25770.0, "atr": 10.0,
    }


class RsiMacdTests(unittest.TestCase):
    def test_rsi_uptrend_above_50_and_bounded(self):
        times = list(range(30))
        closes = [100.0 + i for i in range(30)]
        out = charts_mod.rsi_series(times, closes)
        self.assertEqual(len(out), 16)  # first value after 14 closes
        for p in out:
            self.assertGreaterEqual(p["value"], 0.0)
            self.assertLessEqual(p["value"], 100.0)
        self.assertGreater(out[-1]["value"], 50.0)

    def test_rsi_flat_series_is_50(self):
        out = charts_mod.rsi_series(list(range(20)), [100.0] * 20)
        self.assertEqual(len(out), 6)
        self.assertTrue(all(p["value"] == 50.0 for p in out))

    def test_rsi_too_few_bars_is_empty_not_fabricated(self):
        self.assertEqual(charts_mod.rsi_series(list(range(6)), [100.0] * 6), [])
        self.assertEqual(charts_mod.rsi_series(list(range(14)), [100.0] * 14), [])

    def test_macd_uptrend_positive_and_signal_lags(self):
        times = list(range(40))
        closes = [100.0 + i for i in range(40)]
        macd = charts_mod.macd_series(times, closes)
        self.assertTrue(macd["line"])
        self.assertGreater(macd["line"][-1]["value"], 0.0)
        # Signal needs 9 MACD values, so it appears later than the line.
        self.assertLessEqual(len(macd["signal"]), len(macd["line"]))
        self.assertEqual(len(macd["hist"]), len(macd["signal"]))
        for p in macd["hist"]:
            self.assertIn(p["color"], ("#2f9e5f", "#d6455d"))

    def test_macd_too_few_bars_is_short_not_fabricated(self):
        # 6 closes: the MACD line is seeded from the 6 closes (1 point)
        # and the signal from that 1 MACD value — short, never padded.
        macd = charts_mod.macd_series(list(range(6)), [100.0 + i for i in range(6)])
        self.assertEqual(len(macd["line"]), 1)
        self.assertEqual(len(macd["signal"]), 1)
        self.assertEqual(len(macd["hist"]), 1)


class LevelsChartIndicatorTests(unittest.TestCase):
    def test_levels_chart_carries_rsi_macd_and_buy_marker(self):
        with patch.object(levels_mod, "compute_levels",
                          return_value=_levels_row("long")), \
             patch.object(charts_mod, "fetch_intraday_bars",
                          return_value=_bars_30()):
            payload = charts_mod.levels_chart("^NSEI", "NIFTY")
        self.assertIsNone(payload["error"])
        self.assertEqual(len(payload["candles"]), 30)
        self.assertTrue(payload["rsi"])
        self.assertEqual(payload["rsi_last"], payload["rsi"][-1]["value"])
        self.assertTrue(payload["macd"]["line"])
        self.assertIsNotNone(payload["macd_last"])
        self.assertTrue(payload["overlays"]["rsi"])
        self.assertTrue(payload["overlays"]["macd"])
        # BUY marker on the trigger bar, CALL-green, group "trades".
        buys = [m for m in payload["markers"]
                if m.get("group") == "trades" and m.get("text") == "BUY"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["color"], "#2f9e5f")
        self.assertEqual(buys[0]["shape"], "arrowUp")
        self.assertEqual(buys[0]["position"], "belowBar")

    def test_levels_chart_short_signal_has_sell_marker(self):
        with patch.object(levels_mod, "compute_levels",
                          return_value=_levels_row("short")), \
             patch.object(charts_mod, "fetch_intraday_bars",
                          return_value=_bars_30_short()):
            payload = charts_mod.levels_chart("^NSEI", "NIFTY")
        sells = [m for m in payload["markers"]
                 if m.get("group") == "trades" and m.get("text") == "SELL"]
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["color"], "#d6455d")
        self.assertEqual(sells[0]["shape"], "arrowDown")
        self.assertEqual(sells[0]["position"], "aboveBar")


class OrbZeroHitsTests(unittest.TestCase):
    def test_orb_chart_no_hit_returns_range_chart_not_error(self):
        with patch.object(charts_mod, "fetch_intraday_bars",
                          return_value=_bars_flat()), \
             patch.object(levels_mod, "fetch_daily_bars",
                          return_value=pd.DataFrame(
                              {"high": [25800.0], "low": [25550.0]},
                              index=pd.DatetimeIndex(
                                  [datetime(2026, 11, 25, 9, 15, tzinfo=IST)],
                                  tz=IST))):
            payload = charts_mod.orb_chart("^NSEI", "NIFTY")
        self.assertIsNone(payload["error"])
        self.assertIn("no ORB trigger yet", payload["note"])
        self.assertEqual(len(payload["candles"]), 6)
        labels = {ln["label"] for ln in payload["lines"]}
        self.assertIn("ORB HIGH", labels)
        self.assertIn("ORB LOW", labels)
        self.assertIn("PDH", labels)
        self.assertIn("PDL", labels)

    def test_api_orbchart_no_hit_returns_chart_not_empty_error(self):
        range_payload = {"symbol": "NIFTY",
                         "candles": [{"time": 1, "open": 1, "high": 2,
                                      "low": 1, "close": 2}],
                         "note": "no ORB trigger yet today — range only, chart keeps updating",
                         "error": None}
        with patch.object(app, "_orb_scan",
                          return_value={"hits": [], "failed": [],
                                        "fetched": 39, "usable": 39,
                                        "as_of": "2026-11-27 10:00:00 IST"}), \
             patch.object(app.charts_mod, "orb_chart",
                          return_value=range_payload) as m, \
             patch.object(app, "_cached",
                          side_effect=lambda key, ttl, fn: fn()):
            out = app._api_orbchart({"symbol": ["NIFTY"]})
        self.assertEqual(out, range_payload)
        self.assertTrue(out["candles"])
        self.assertIsNone(out["error"])
        m.assert_called_once()
        self.assertEqual(m.call_args[0][0], "^NSEI")

    def test_orb_page_defaults_to_nifty_when_zero_hits(self):
        js = app._chart_js()
        self.assertIn("pick(d.top_symbol||'NIFTY')", js)


class OverlayPillsTests(unittest.TestCase):
    def test_levels_page_has_overlay_pills_including_rsi_macd(self):
        html = app.levels_page("hi")
        self.assertIn("id='levels-chart-pills'", html)
        for key in ("orderblock", "fvg", "structure", "vwap", "ema",
                    "rsi", "macd", "volume", "trades"):
            self.assertIn(f"data-chart='levels-chart' data-ov='{key}'", html)
        self.assertIn("bhanuToggleOverlay('levels-chart','rsi'", html)
        self.assertIn("bhanuToggleOverlay('levels-chart','macd'", html)

    def test_orb_page_has_overlay_pills_including_rsi_macd(self):
        html = app.orb_page("hi")
        self.assertIn("id='orb-chart-pills'", html)
        for key in ("rsi", "macd", "ema", "vwap", "volume"):
            self.assertIn(f"data-chart='orb-chart' data-ov='{key}'", html)

    def test_stock_page_has_rsi_macd_pills(self):
        html = app.stock_page("hi", {"symbol": ["RELIANCE"]})
        self.assertIn("data-chart='stock-chart' data-ov='rsi'", html)
        self.assertIn("data-chart='stock-chart' data-ov='macd'", html)
        self.assertIn(">RSI<", html)
        self.assertIn(">MACD<", html)

    def test_chart_js_has_rsi_macd_panes_and_pill_sync(self):
        js = app._chart_js()
        self.assertIn("priceScaleId:'rsi'", js)
        self.assertIn("priceScaleId:'macd'", js)
        self.assertIn("function bhanuSyncPills(id)", js)
        self.assertIn("RSI 14: ", js)
        self.assertIn("MACD: ", js)
        # 30 / 70 reference lines on the RSI pane.
        self.assertIn("price:70", js)
        self.assertIn("price:30", js)
        # Pane layout: price scale margins move when panes have data.
        self.assertIn("priceScale('right').applyOptions({scaleMargins:{top:0,bottom:priceBottom}})", js)


class SectorLabelCollisionTests(unittest.TestCase):
    def test_sector_render_has_collision_placement(self):
        js = app._chart_js()
        self.assertIn("hitsPlaced", js)
        self.assertIn("overlapOf", js)
        self.assertIn("showPct", js)
        # Labels try inside / above / below / left / right of the bubble.
        self.assertIn("nameDy:-r-20", js)
        self.assertIn("nameDy:r+10", js)


class FlowCadenceTests(unittest.TestCase):
    def test_poller_flow_refresh_is_120s(self):
        self.assertEqual(poller_mod.POLL_FLOW_FAST_S, 120)
        # Slow (market closed) cadence is unchanged.
        self.assertEqual(poller_mod.POLL_FLOW_SLOW_S, 600)

    def test_api_flow_fallback_cache_is_120s(self):
        import inspect
        src = inspect.getsource(app._api_flow)
        self.assertIn('_cached("api:flow", 120,', src)
        src = inspect.getsource(app._api_sectors)
        self.assertIn('_cached("api:flow", 120,', src)


class ChartLoadSpeedTests(unittest.TestCase):
    def test_detail_poll_interval_is_500ms(self):
        js = app._chart_js()
        self.assertIn("setTimeout(resolve,500)", js)
        self.assertNotIn("setTimeout(resolve,1200)", js)

    def test_api_stock_uses_spark_quote_for_index(self):
        from bhanu import stock as stock_mod
        series = [{"symbol": "NIFTY", "line": [], "change_pct": 0.1,
                   "up": True, "is_index": True, "sector": "Index",
                   "traded_value": 1.0}]
        row = _levels_row("long")
        sig = {"symbol": "NIFTY", "direction": "long", "entry": 25725.0,
               "stop": 25702.5, "tp1": 25747.5, "tp2": 25770.0,
               "tp3": 25792.5, "decision": "ENTER", "summary": "test"}
        hist = {"available": False, "reason": "no data", "trades": []}
        chart = {"symbol": "NIFTY",
                 "candles": [{"time": 1, "open": 1, "high": 1, "low": 1,
                              "close": 1}],
                 "vwap": [], "lines": [], "markers": [], "boxes": [],
                 "overlays": {"orderblock": True, "fvg": True,
                              "structure": True, "liquidity": False,
                              "vwap": True, "trades": False},
                 "error": None}
        spark = {"ltp": 25840.0, "prev": 25884.5, "time": 1795754000}
        # Bypass app._cached: the whole stock result is cached 120s,
        # so without this an earlier test's cached quote would win.
        with patch.object(app, "_cached",
                          side_effect=lambda key, ttl, fn: fn()), \
             patch.object(poller_mod, "get_spark",
                          return_value=spark), \
             patch.object(poller_mod, "get_prev_close",
                          return_value=25884.5), \
             patch.object(poller_mod, "get_flow",
                          return_value={"series": series}), \
             patch.object(levels_mod, "compute_levels",
                          return_value=row), \
             patch.object(app, "_entry_signal_for", return_value=sig), \
             patch.object(stock_mod, "orb_history", return_value=hist), \
             patch.object(charts_mod, "stock_chart", return_value=chart), \
             patch.object(data_mod, "fetch_quote") as fq:
            result = app._api_stock({"symbol": ["NIFTY"], "interval": ["5m"]})
        self.assertIsNone(result["error"])
        fq.assert_not_called()
        self.assertIn("25,840.00", result["header_html"])


class ContinuousMotionTests(unittest.TestCase):
    """The home charts must glide continuously — no 2s brake, no snap.

    Root causes fixed: (a) the old per-tick ease-in-out glide
    decelerated to a full stop at the end of every 1900ms glide;
    (b) the 1900ms glide was shorter than the 2000ms tick + fetch
    latency, leaving a dead beat; (c) a new glide started from the
    previous TARGET, so a late fetch snapped the line; (d) the top
    sparklines were fully redrawn (innerHTML) once per tick.
    The fix is one persistent rAF loop per chart, linear motion,
    a glide longer than the tick, retargeting from the displayed
    value, and per-frame sparkline last-point updates.
    """

    def _js(self):
        return app._chart_js()

    def _fn(self, js, name, end_marker=None):
        start = js.index("function %s" % name)
        if end_marker:
            end = js.index(end_marker, start)
        else:
            end = js.index("\nfunction ", start + 10)
        return js[start:end]

    def test_old_ease_in_out_animation_is_gone(self):
        js = self._js()
        self.assertNotIn("bhanuAnimLast", js)
        self.assertNotIn("bhanuEase", js)
        self.assertNotIn("/1900", js)

    def test_glide_duration_covers_the_tick_interval(self):
        import re
        js = self._js()
        tick = int(re.search(r"var BHANU_TICK_MS=(\d+);", js).group(1))
        glide = int(re.search(r"var BHANU_GLIDE_MS=(\d+);", js).group(1))
        self.assertEqual(tick, 2000)
        # Glide must outlast the tick so the line is still moving
        # when the next target lands — never a dead beat.
        self.assertGreater(glide, tick)
        self.assertIn("setInterval(run,BHANU_TICK_MS)", js)

    def test_tick_path_updates_last_point_not_rebuild(self):
        js = self._js()
        tick_fn = self._fn(js, "bhanuLiveLineTick", "function bhanuStartLiveLine")
        self.assertIn("bhanuLineRetarget", tick_fn)
        self.assertNotIn("setData", tick_fn)
        # The animator itself appends/updates the last point only.
        anim_fn = self._fn(js, "bhanuLineAnim", "function bhanuLineRetarget")
        self.assertIn("st.series.update({time:st.animTime||st.lastTime,value:v})",
                      anim_fn)
        self.assertNotIn("setData", anim_fn)

    def test_animator_is_one_persistent_loop_retargeting_from_displayed(self):
        js = self._js()
        anim_fn = self._fn(js, "bhanuLineAnim", "function bhanuLineRetarget")
        self.assertIn("st.animRunning", anim_fn)
        self.assertIn("requestAnimationFrame", anim_fn)
        # New target starts from the value currently on screen.
        retarget_fn = self._fn(js, "bhanuLineRetarget", "function bhanuSpark")
        self.assertIn("st.animFrom=st.displayed;", retarget_fn)
        self.assertIn("bhanuLineAnim(id,symbol);", retarget_fn)

    def test_glide_is_linear_constant_speed(self):
        js = self._js()
        anim_fn = self._fn(js, "bhanuLineAnim", "function bhanuLineRetarget")
        self.assertIn("var k=Math.min(1,(now-st.animStart)/BHANU_GLIDE_MS);",
                      anim_fn)
        self.assertIn("var v=st.animFrom+(st.animTo-st.animFrom)*k;",
                      anim_fn)
        # No easing function in the motion path (easing is the brake).
        self.assertNotIn("Ease", anim_fn)

    def test_sparkline_moves_last_point_per_frame_not_full_redraw(self):
        js = self._js()
        self.assertIn("function bhanuSparkLast(id, v)", js)
        spark_fn = self._fn(js, "bhanuSparkLast", "function bhanuSparkFor")
        self.assertIn("setAttribute('d',d)", spark_fn)
        self.assertNotIn("innerHTML", spark_fn)
        # The per-tick spark refresh skips the rebuild when the 5m
        # bar is unchanged — the animator moves the last point.
        for_fn = self._fn(js, "bhanuSparkFor", "function bhanuFlowRender")
        self.assertIn("pts[pts.length-1].time===line[line.length-1].time",
                      for_fn)
        # The animator drives the sparkline every frame.
        anim_fn = self._fn(js, "bhanuLineAnim", "function bhanuLineRetarget")
        self.assertIn("bhanuSparkLast('q-'+slug+'-spark',v);", anim_fn)

    def test_full_refresh_reseats_animator_without_snap(self):
        js = self._js()
        start = js.index("if(ticks%150===0){")
        refresh = js[start:start + 1500]
        self.assertIn("st.displayed=nl[nl.length-1].value;", refresh)
        self.assertIn("st.animTo=nl[nl.length-1].value;", refresh)
        self.assertIn("st.animFrom=nl[nl.length-1].value;", refresh)


if __name__ == "__main__":
    unittest.main()
