"""tests/test_dashboard_fixes.py — regression tests for the 27 Nov 2026 fixes.

1. Levels page: symbol selector (indices + all F&O stocks) + levelschart
   accepts any UNIVERSE symbol, not just the 3 indices.
2. Paper Trading page: symbol dropdown + live-price line + /api/quote.
3. Home intraday charts: gradient area series (lineWidth 3), not a plain
   thin line series.

Offline: all network access is mocked.
"""
import unittest
from unittest.mock import patch

from web import app


class LevelsSelectorTests(unittest.TestCase):
    def test_levels_page_has_symbol_selector(self):
        html = app.levels_page("hi")
        self.assertIn("id='levels-symbol-select'", html)
        self.assertIn("onchange=\"bhanuLevelsPick(this.value)\"", html)
        for sym in ("NIFTY", "BANKNIFTY", "SENSEX", "RELIANCE", "HDFCBANK",
                    "SBIN", "TATASTEEL", "HINDALCO"):
            self.assertIn(f"<option value='{sym}'", html)

    def test_levels_page_preselects_query_symbol(self):
        html = app.levels_page("hi", {"symbol": ["BANKNIFTY"]})
        self.assertIn("<option value='BANKNIFTY' selected>", html)
        self.assertIn("id='levels-chart-heading'", html)

    def test_levelschart_accepts_stock_symbol(self):
        payload = {"symbol": "RELIANCE", "candles": [{"time": 1, "open": 1,
                    "high": 2, "low": 1, "close": 2}], "levels": []}
        with patch.object(app.charts_mod, "levels_chart",
                          return_value=payload) as m, \
             patch.object(app, "_cached",
                          side_effect=lambda key, ttl, fn: fn()):
            out = app._api_levelschart({"symbol": ["RELIANCE"]})
        self.assertEqual(out, payload)
        m.assert_called_once()
        self.assertEqual(m.call_args[0][0], "RELIANCE.NS")
        self.assertEqual(m.call_args[0][1], "RELIANCE")

    def test_levelschart_rejects_unknown_symbol(self):
        out = app._api_levelschart({"symbol": ["NOPE"]})
        self.assertEqual(out["candles"], [])
        self.assertIn("unknown symbol", out["error"])

    def test_chart_js_exposes_levels_pick(self):
        js = app._chart_js()
        self.assertIn("window.bhanuLevelsPick=pick", js)
        self.assertIn("disposeLevelsChart", js)
        self.assertIn("_bhanuChartId", js)


class PaperDeskTests(unittest.TestCase):
    def test_paper_page_has_symbol_dropdown_and_live_price(self):
        html = app.paper_page("hi")
        self.assertIn("<select id='pro-paper-symbol'", html)
        self.assertIn("onchange='BhanuPro.paperSymbolChanged()'", html)
        self.assertIn("id='pro-paper-live-price'", html)
        for sym in ("NIFTY", "BANKNIFTY", "SENSEX", "RELIANCE", "INFY"):
            self.assertIn(f"<option value='{sym}'", html)
        # Order buttons + qty/entry/SL fields unchanged.
        self.assertIn("id='btn-buy-call'", html)
        self.assertIn("id='btn-buy-put'", html)
        self.assertIn("id='pro-paper-qty'", html)
        self.assertIn("id='pro-paper-price'", html)
        self.assertIn("id='pro-paper-sl'", html)

    def test_api_quote_stock_uses_cached_quote(self):
        with patch.object(app.data_mod, "fetch_quote",
                          return_value={"price": 1234.5, "prev_close": 1200.0,
                                        "change_pct": 2.88}) as m:
            out = app._api_quote({"symbol": ["RELIANCE"]})
        self.assertEqual(out["price"], 1234.5)
        self.assertEqual(out["symbol"], "RELIANCE")
        self.assertIsNone(out["error"])
        m.assert_called_once_with("RELIANCE.NS")

    def test_api_quote_unknown_symbol_errors(self):
        out = app._api_quote({"symbol": ["NOPE"]})
        self.assertIsNone(out["price"])
        self.assertIn("unknown symbol", out["error"])

    def test_api_quote_index_uses_poller_spark_when_live(self):
        spark = {"ltp": 25840.0, "prev": 25884.5, "time": 1795754000}
        with patch.object(app.poller_mod, "get_spark",
                          return_value=spark), \
             patch.object(app.poller_mod, "get_prev_close",
                          return_value=25884.5), \
             patch.object(app.data_mod, "fetch_quote") as fq:
            out = app._api_quote({"symbol": ["NIFTY"]})
        self.assertEqual(out["price"], 25840.0)
        self.assertIsNone(out["error"])
        fq.assert_not_called()


class HomeChartQualityTests(unittest.TestCase):
    def test_home_line_chart_is_area_series(self):
        js = app._chart_js()
        self.assertIn("addAreaSeries({lineColor:col,topColor:bhanuAreaTop(col)",
                      js)
        self.assertIn("lineWidth:3", js)
        self.assertIn("function bhanuAreaTop(hex)", js)
        self.assertIn("applyOptions({lineColor:nc,topColor:bhanuAreaTop(nc)})",
                      js)
        # The old plain thin line series for the home intraday chart is gone.
        self.assertNotIn("addLineSeries({color:col,lineWidth:2})", js)
        # Cleaner axes: no hard scale borders.
        self.assertIn("rightPriceScale:{borderVisible:false}", js)
        self.assertIn("borderVisible:false", js)


if __name__ == "__main__":
    unittest.main()
