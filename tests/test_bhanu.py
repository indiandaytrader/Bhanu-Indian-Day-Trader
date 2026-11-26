"""Offline unit tests for Bhanu Indian Day Trader (no network, synthetic data).

Run:  python3 -m unittest discover -s tests -v
"""

import sys
import unittest
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from bhanu import alerts, config as config_mod, data as data_mod
from bhanu import charts as charts_mod
from bhanu import entry as entry_mod
from bhanu import exit as exit_mod
from bhanu import i18n as i18n_mod
from bhanu import levels as levels_mod
from bhanu import news as news_mod
from bhanu import paper as paper_mod
from bhanu import risk as risk_mod
from bhanu import scanner as scanner_mod
from bhanu import signal as signal_mod
from bhanu import sizing as sizing_mod
from bhanu import skills as skills_mod
from bhanu import structure as structure_mod
from bhanu import timecheck as timecheck_mod
from bhanu import timemachine as tm_mod
from bhanu import volume as volume_mod

IST = ZoneInfo("Asia/Kolkata")


def _bars_5m():
    """Synthetic NIFTY 5m bars for 26 Nov 2026, 09:15-10:00 IST."""
    base = datetime(2026, 11, 26, 9, 15, tzinfo=IST)
    rows = [
        # (minutes offset, open, high, low, close, volume)
        (0, 25700, 25718, 25687, 25710, 1000),
        (5, 25710, 25715, 25700, 25705, 900),
        (10, 25705, 25712, 25695, 25708, 1100),
        (15, 25708, 25720, 25705, 25718, 1200),
        (20, 25718, 25735, 25715, 25730, 1500),  # closes above range high 25718
        (25, 25730, 25740, 25725, 25738, 1300),
    ]
    idx = pd.DatetimeIndex(
        [base + pd.Timedelta(minutes=m) for m, *_ in rows], tz=IST
    )
    return pd.DataFrame(
        {
            "open": [r[1] for r in rows],
            "high": [r[2] for r in rows],
            "low": [r[3] for r in rows],
            "close": [r[4] for r in rows],
            "volume": [r[5] for r in rows],
        },
        index=idx,
    )


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = config_mod.load_config()
        self.assertTrue(cfg.paper)
        self.assertEqual(cfg.budget, 100000)
        self.assertEqual(cfg.max_loss_per_trade, 500)
        self.assertEqual(cfg.max_trades_per_day, 3)
        self.assertEqual(cfg.min_rr, 1.5)
        self.assertEqual(cfg.flat_by, "15:15")


class TestScanner(unittest.TestCase):
    def test_daily_setups(self):
        bars = data_mod.make_synthetic_bars(30)
        setups = scanner_mod.daily_setups(bars)
        self.assertIsInstance(setups, dict)
        self.assertIn("breakout_20d", setups)

    def test_orb_range_and_hit(self):
        bars = _bars_5m()
        rng = scanner_mod.orb_range(bars)
        self.assertEqual(rng, (25718.0, 25687.0))
        hit = scanner_mod.orb_hit(bars)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["direction"], "long")
        self.assertEqual(hit["close"], 25730.0)

    def test_wick_does_not_trigger(self):
        # A bar whose HIGH is above the range but CLOSE is inside must not hit.
        bars = _bars_5m().iloc[:5].copy()
        bars.iloc[-1, bars.columns.get_loc("high")] = 25750.0  # wick above
        bars.iloc[-1, bars.columns.get_loc("close")] = 25710.0  # close inside
        hit = scanner_mod.orb_hit(bars)
        self.assertIsNone(hit)


class TestEntryExit(unittest.TestCase):
    def test_orb_entry_long(self):
        plan = entry_mod.plan_orb_entry(_bars_5m(), "long")
        self.assertIsNotNone(plan)
        self.assertEqual(plan.trigger_price, 25730.0)
        self.assertEqual(plan.stop, 25702.5)  # range midpoint
        self.assertEqual(plan.target, 25785.0)  # 2R

    def test_exit_refuses_bad_stop(self):
        # stop == entry must be refused
        with self.assertRaises(ValueError):
            exit_mod.compute_exit(25730.0, 25730.0, 25730.0, rule="or-mid")

    def test_stop_never_widens(self):
        self.assertTrue(exit_mod.check_stop_move(25690, 25700, "long"))  # tighten ok
        self.assertFalse(exit_mod.check_stop_move(25690, 25680, "long"))  # widen no


class TestRiskSizing(unittest.TestCase):
    def test_four_limit_sizing(self):
        cfg = risk_mod.default_config()
        self.assertEqual(cfg.min_rr, 1.5)
        qty = risk_mod.size_position(226.55, 176.55, cfg)
        self.assertEqual(qty, 10)  # 500 / 50

    def test_sizing_report(self):
        cfg = sizing_mod.default_config()
        report = sizing_mod.size_report(226.55, 176.55, cfg)
        self.assertEqual(report["qty"], 10)
        self.assertEqual(report["verdict"], "PASS")
        self.assertAlmostEqual(report["rr"], 2.0)

    def test_refuse_when_stop_too_far(self):
        cfg = sizing_mod.default_config(max_loss_per_trade=150.0)
        report = sizing_mod.size_report(226.55, 26.55, cfg)
        self.assertEqual(report["verdict"], "REFUSE")
        self.assertEqual(report["qty"], 0)

    def test_daily_loss_counts_full_max_loss(self):
        entries = [{"max_loss": 500.0}, {"max_loss": 100.0}]
        self.assertEqual(risk_mod.daily_loss_used(entries), 600.0)

    def test_gate_blocks_no_stop(self):
        cfg = risk_mod.default_config()
        order = {"symbol": "NIFTY", "direction": "long", "qty": 10,
                 "entry": 25730.0, "stop": None, "target": 25785.0,
                 "asset": "index", "time": dtime(10, 0)}
        verdict, _ = risk_mod.gate(order, cfg, {"entries_today": [], "trades_today": 0})
        self.assertEqual(verdict, "BLOCK")

    def test_gate_exit_never_blocked_by_day_limits(self):
        cfg = risk_mod.default_config()
        order = {"symbol": "NIFTY", "asset": "index", "side": "sell",
                 "direction": "long", "qty": 10}
        day_state = {"entries_today": [], "trades_today": 3}  # day limits used up
        verdict, _ = risk_mod.gate_exit(order, cfg, day_state)
        self.assertEqual(verdict, "PASS")


class TestAlerts(unittest.TestCase):
    def test_alerts_fire_once(self):
        plan = alerts.AlertPlan(symbol="NIFTY", direction="long",
                                stop=25700.0, target=25900.0, flat_by="15:15")
        built = alerts.build_alerts(plan)
        fired: set = set()
        now = datetime(2026, 11, 26, 13, 0, tzinfo=IST)
        first = alerts.check_alerts(built, 25905.0, now, fired)
        self.assertTrue(any(a["kind"] == "target" for a in first))
        second = alerts.check_alerts(built, 25905.0, now, fired)
        self.assertFalse(any(a["kind"] == "target" for a in second))


class TestPaper(unittest.TestCase):
    def test_open_close_ledger(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "paper.json"
            pos = paper_mod.open_position("NIFTY 25800 CE", "long", 65, 120.0,
                                          90.0, 180.0, state_path=path)
            self.assertEqual(len(paper_mod.list_positions(path)), 1)
            closed = paper_mod.close_position(pos["id"], 165.0, "target", state_path=path)
            self.assertAlmostEqual(closed["realised_pnl"], 65 * 45.0)
            summary = paper_mod.day_summary(datetime.now(IST).date(), state_path=path)
            self.assertTrue(summary["flat"])


class TestTimemachine(unittest.TestCase):
    def test_verdict_bands(self):
        v = tm_mod.verdict(30.0, 25.0, 2.0)
        self.assertIn(v["verdict"], ("coin flip", "leans", "strongly"))
        # break-even for 2R is 33.3% of decisive; 30/(30+25)=54.5% -> leans/strongly
        self.assertGreater(v["edge_pts"], 5)

    def test_monte_carlo_synthetic(self):
        bars = tm_mod.synthetic_bars(sessions=25, seed=7)
        out = tm_mod.monte_carlo(bars, entry=25000, stop=24900, target=25200,
                                 direction="long", n=200, seed=19)
        total = out["target_first_pct"] + out["stop_first_pct"] + out["neither_pct"]
        self.assertAlmostEqual(total, 100.0, places=1)


class TestI18n(unittest.TestCase):
    def test_hinglish_strings(self):
        self.assertEqual(i18n_mod.t("flat_by", "hi"), "15:15 tak flat")
        self.assertEqual(i18n_mod.t("flat_by", "en"), "Flat by 15:15")
        self.assertEqual(i18n_mod.t("nonexistent_key", "hi"), "nonexistent_key")

    def test_levels_and_skills_keys(self):
        self.assertEqual(i18n_mod.t("levels", "hi"), "Levels")
        self.assertEqual(i18n_mod.t("levels", "en"), "Levels")
        self.assertEqual(i18n_mod.t("skills", "hi"), "Skills")
        self.assertEqual(i18n_mod.t("skills", "en"), "Skills")


def _levels_5m_bars():
    """Synthetic 5m bars: yesterday + today (today = actual current date).

    Today's bars mirror _bars_5m(): ORB range high 25718 / low 25687,
    long trigger at the 09:35 bar (close 25730).
    """
    today = datetime.now(IST).date()
    yesterday = today - pd.Timedelta(days=1)
    rows = []
    # Yesterday: a quiet range with one clear swing high/low.
    y_base = datetime.combine(yesterday, datetime.min.time().replace(hour=9, minute=15), tzinfo=IST)
    y_closes = [25600, 25620, 25640, 25630, 25610, 25650, 25645, 25620, 25600, 25590, 25605, 25615]
    for i, c in enumerate(y_closes):
        ts = y_base + pd.Timedelta(minutes=5 * i)
        rows.append((ts, c - 5, c + 8, c - 8, c, 1000))
    # Today: ORB range + long trigger.
    t_base = datetime.combine(today, datetime.min.time().replace(hour=9, minute=15), tzinfo=IST)
    t_rows = [
        (0, 25700, 25718, 25687, 25710, 1000),
        (5, 25710, 25715, 25700, 25705, 900),
        (10, 25705, 25712, 25695, 25708, 1100),
        (15, 25708, 25720, 25705, 25718, 1200),
        (20, 25718, 25735, 25715, 25730, 1500),  # trigger: close above 25718
        (25, 25730, 25740, 25725, 25738, 1300),
        (30, 25738, 25745, 25730, 25742, 1200),
        (35, 25742, 25750, 25735, 25748, 1100),
    ]
    for m, o, h, l, c, v in t_rows:
        rows.append((t_base + pd.Timedelta(minutes=m), o, h, l, c, v))
    idx = pd.DatetimeIndex([r[0] for r in rows], tz=IST)
    return pd.DataFrame(
        {
            "open": [r[1] for r in rows],
            "high": [r[2] for r in rows],
            "low": [r[3] for r in rows],
            "close": [r[4] for r in rows],
            "volume": [r[5] for r in rows],
        },
        index=idx,
    )


def _levels_daily_bars():
    """Synthetic daily bars: day-2, day-1, today (partial)."""
    today = datetime.now(IST).date()
    dates = [today - pd.Timedelta(days=2), today - pd.Timedelta(days=1), today]
    rows = [
        (25600, 25400, 25500),  # day-2
        (25800, 25550, 25700),  # day-1 -> PDH 25800 / PDL 25550
        (25750, 25680, 25730),  # today (partial)
    ]
    idx = pd.DatetimeIndex(
        [datetime.combine(d, datetime.min.time().replace(hour=9, minute=15), tzinfo=IST) for d in dates],
        tz=IST,
    )
    return pd.DataFrame(
        {
            "open": [r[2] for r in rows],
            "high": [r[0] for r in rows],
            "low": [r[1] for r in rows],
            "close": [r[2] for r in rows],
            "volume": [1000, 1200, 900],
        },
        index=idx,
    )


class TestLevels(unittest.TestCase):
    def test_swing_high_low(self):
        bars = _levels_5m_bars()
        highs, lows = levels_mod.swing_levels(bars)
        # Yesterday's 25650 close bar has high 25658, a local max.
        self.assertIn(25658.0, highs)
        # Yesterday's 25590 close bar has low 25582, a local min.
        self.assertIn(25582.0, lows)

    def test_swing_empty_bars(self):
        highs, lows = levels_mod.swing_levels(pd.DataFrame(columns=["open", "high", "low", "close", "volume"]))
        self.assertEqual(highs, [])
        self.assertEqual(lows, [])

    def test_prev_day_high_low_uses_completed_session(self):
        # Last daily bar is today's partial bar -> PDH/PDL come from day-1.
        pdh_pdl = levels_mod.prev_day_high_low(_levels_daily_bars())
        self.assertEqual(pdh_pdl, (25800.0, 25550.0))

    def test_prev_day_high_low_last_bar_is_previous_day(self):
        bars = _levels_daily_bars().iloc[:2]  # last bar is day-1, not today
        self.assertEqual(levels_mod.prev_day_high_low(bars), (25800.0, 25550.0))

    def test_nearest_levels(self):
        support, resistance = levels_mod.nearest_levels(25700.0, [25600.0, 25650.0, 25800.0, 25900.0])
        self.assertEqual(support, 25650.0)
        self.assertEqual(resistance, 25800.0)
        support, resistance = levels_mod.nearest_levels(25700.0, [25800.0])
        self.assertIsNone(support)
        self.assertEqual(resistance, 25800.0)

    def test_cluster_levels_merges_near_duplicates(self):
        clustered = levels_mod.cluster_levels([25700.0, 25702.0, 25800.0], 25700.0)
        # 25700 and 25702 are within 0.05% of 25700 (12.85) -> merged to mean.
        self.assertEqual(len(clustered), 2)
        self.assertAlmostEqual(clustered[0], 25701.0)
        self.assertEqual(clustered[1], 25800.0)

    def test_atr_wilder_positive(self):
        atr = levels_mod.atr_wilder(_levels_5m_bars())
        self.assertIsNotNone(atr)
        self.assertGreater(atr, 0)

    def test_compute_levels_offline(self):
        # Monkeypatch the fetches so no network is touched.
        from unittest.mock import patch
        bars_5m = _levels_5m_bars()
        daily = _levels_daily_bars()
        with patch.object(levels_mod, "fetch_intraday_bars", return_value=bars_5m), \
             patch.object(levels_mod, "fetch_daily_bars", return_value=daily):
            row = levels_mod.compute_levels("NIFTY", "^NSEI")
        self.assertEqual(row["symbol"], "NIFTY")
        self.assertEqual(row["price"], 25748.0)
        self.assertEqual(row["pdh"], 25800.0)
        self.assertEqual(row["pdl"], 25550.0)
        self.assertEqual(row["orb_high"], 25718.0)
        self.assertEqual(row["orb_low"], 25687.0)
        # Nearest support is below the price, resistance above it.
        self.assertLess(row["nearest_support"], row["price"])
        self.assertGreater(row["nearest_resistance"], row["price"])
        # ORB long plan: entry = trigger close, stop = range midpoint, 2R target.
        self.assertEqual(row["signal"], "long")
        self.assertEqual(row["entry"], 25730.0)
        self.assertEqual(row["stop"], 25702.5)
        self.assertEqual(row["target"], 25785.0)
        self.assertIsNotNone(row["atr"])
        self.assertGreater(row["atr"], 0)


class TestSkills(unittest.TestCase):
    def test_skill_defs_has_all_11(self):
        names = [s["daytrader"] for s in skills_mod.SKILL_DEFS]
        self.assertEqual(len(names), 11)
        self.assertEqual(
            set(names),
            {
                "trader-scanner", "trader-news", "trader-entry", "trader-exit",
                "trader-timemachine", "trader-budget", "trader-risk",
                "trader-broker", "trader-alerts", "trader-analyst",
                "trader-pelosi",
            },
        )

    def test_collect_offline_returns_11_rows(self):
        rows = skills_mod.collect_skill_status(live=False)
        self.assertEqual(len(rows), 11)
        for row in rows:
            for key in ("daytrader", "module", "does", "shows", "status"):
                self.assertIn(key, row)
                self.assertTrue(row[key])
        by_name = {r["daytrader"]: r for r in rows}
        # Budget is folded into sizing; Pelosi is skipped — both stated plainly.
        self.assertIn("Folded", by_name["trader-budget"]["status"])
        self.assertIn("Skipped", by_name["trader-pelosi"]["status"])
        self.assertIn("FOLDED", by_name["trader-budget"]["does"])

    def test_risk_gate_status_not_garbled(self):
        # Regression: gate() returns (verdict, reason-string); joining the
        # reason as a list produced "m; i; s; s; i; n; g; ..." output.
        plan = {
            "signal": "long", "entry": 25730.0, "stop": 25702.5,
            "target": 25785.0, "price": 25730.0,
            "range_high": 25718.0, "range_low": 25687.0,
        }
        status = skills_mod._risk_gate_status(plan)
        self.assertNotIn("m; i; s", status)
        self.assertIn("qty", status)
        self.assertTrue(status.startswith("PASS") or status.startswith("BLOCK"))


class TestNews(unittest.TestCase):
    def test_check_symbol_returns_latest_even_after_dedupe(self):
        # "latest" keeps the most recent fetched items visible for the
        # dashboard; "items" is the new/unseen set only (dedupe).
        import tempfile
        from unittest.mock import patch
        nse_items = [{
            "source": "NSE announcement", "title": "Reliance Q2 results",
            "link": "https://example.com", "published": "26-Nov-2026",
            "tags": ["earnings"],
        }]
        yahoo_items = [{
            "source": "Yahoo headline", "title": "Nifty hits record high",
            "link": "https://example.com/2", "published": "26 Nov 2026",
            "tags": [],
        }]
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "news_state.json"
            patches = [
                patch.object(news_mod, "STATE_PATH", state_path),
                patch.object(news_mod, "fetch_nse_announcements", return_value=(nse_items, "")),
                patch.object(news_mod, "fetch_yahoo_headlines", return_value=(yahoo_items, "")),
                patch.object(news_mod, "fetch_bse_announcements", return_value=([], "bse note")),
                patch("bhanu.data.fetch_quote", return_value={
                    "price": 25900.0, "prev_close": 25800.0, "change_pct": 0.39,
                }),
            ]
            for p in patches:
                p.start()
            try:
                first = news_mod.check_symbol("NIFTY")
                second = news_mod.check_symbol("NIFTY")
            finally:
                for p in patches:
                    p.stop()
        # First run: both items are new.
        self.assertEqual(len(first["items"]), 2)
        self.assertEqual(len(first["latest"]), 2)
        self.assertEqual(first["latest"][0]["title"], "Reliance Q2 results")
        # Second run: deduped (no new items) but latest still shows them.
        self.assertEqual(len(second["items"]), 0)
        self.assertEqual(len(second["latest"]), 2)

    def test_yahoo_search_fallback_when_rss_empty(self):
        # The per-symbol RSS channel is often empty for Indian symbols;
        # fetch_yahoo_headlines must then fall back to the search API.
        import json as _json
        from unittest.mock import patch
        rss_empty = (
            b'<?xml version="1.0"?><rss version="2.0">'
            b"<channel><title>x</title></channel></rss>"
        )
        search_payload = _json.dumps({"news": [
            {"title": "Nifty rallies on FII buying",
             "link": "https://example.com/1",
             "providerPublishTime": 1795432080, "publisher": "Test"},
            {"title": "RBI keeps repo rate unchanged",
             "link": "https://example.com/2",
             "providerPublishTime": 1795430000, "publisher": "Test"},
        ]}).encode("utf-8")

        def fake_get(url, headers=None):
            if "rss/headline" in url:
                return rss_empty
            return search_payload

        with patch.object(news_mod, "_http_get", side_effect=fake_get):
            items, note = news_mod.fetch_yahoo_headlines("NIFTY")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "Nifty rallies on FII buying")
        self.assertEqual(items[0]["source"], "Yahoo headline")
        self.assertEqual(note, "")


class TestTimecheck(unittest.TestCase):
    def test_trade_check_offline(self):
        setup = {
            "symbol": "NIFTY", "signal": "short", "entry": 25752.50,
            "stop": 25822.33, "target": 25612.84, "price": 25893.15,
            "range_high": 25876.0, "range_low": 25768.65,
        }
        tc = timecheck_mod.trade_check(setup)
        self.assertTrue(tc["available"])
        self.assertEqual(tc["symbol"], "NIFTY")
        self.assertEqual(tc["direction"], "short")
        self.assertIn(tc["gate_verdict"], ("PASS", "BLOCK"))
        self.assertIsInstance(tc["gate_reason"], str)
        for key in ("max_loss", "position_cap", "max_order", "cash_buffer"):
            self.assertIn(key, tc["limits"])
        # qty is the four-limit minimum (floored to whole units).
        self.assertEqual(tc["qty"], min(tc["limits"].values()))
        self.assertGreater(tc["qty"], 0)
        self.assertAlmostEqual(tc["rr"], 2.0, places=1)
        self.assertGreaterEqual(tc["positions_max"], 0)
        self.assertEqual(tc["positions_by_day"], 3)

    def test_trade_check_no_setup(self):
        tc = timecheck_mod.trade_check({})
        self.assertFalse(tc["available"])
        self.assertIn("reason", tc)

    def test_monte_carlo_summary_no_setup(self):
        result = timecheck_mod.monte_carlo_summary({})
        self.assertFalse(result["available"])
        self.assertIn("reason", result)

    def test_best_orb_setup_no_signal(self):
        from unittest.mock import patch
        with patch.object(levels_mod, "scan_levels", return_value={
            "levels": [{"symbol": "NIFTY", "signal": None}], "failed": [],
        }):
            result = timecheck_mod.best_orb_setup()
        self.assertIn("error", result)


class TestDashboardNews(unittest.TestCase):
    """Home page shows headlines AUTOMATICALLY (bot checked) — no manual check."""

    def _mock_news(self):
        return {
            "symbol": "NIFTY",
            "moving": False,
            "change_pct": 0.39,
            "tags": [],
            "items": [{
                "source": "Yahoo headline", "title": "Nifty rallies on FII buying",
                "link": "https://example.com/1", "published": "26 Nov 2026",
                "tags": [],
            }],
            "latest": [
                {"source": "Yahoo headline", "title": "Nifty rallies on FII buying",
                 "link": "https://example.com/1", "published": "26 Nov 2026", "tags": []},
                {"source": "Yahoo headline", "title": "RBI keeps repo rate unchanged",
                 "link": "https://example.com/2", "published": "26 Nov 2026", "tags": []},
            ],
            "sources": {
                "nse_announcements": "NSE announcements unavailable: HTTP Error 403: Forbidden",
                "bse_announcements": "BSE announcements need a BSE scrip code",
                "yahoo_headlines": "5 item(s)",
            },
            "quote_error": "",
        }

    def test_home_shows_latest_headlines_automatically(self):
        from unittest.mock import patch
        from web import app as app_mod
        app_mod._cache.clear()
        with patch.object(news_mod, "check_symbol", return_value=self._mock_news()):
            # Headlines arrive via /api/news after the page loads; the
            # home page itself carries the news-feed slot.
            out = app_mod._api_news({}, "hi")
            page = app_mod.home("hi")
        self.assertIn("news-feed", page)
        # Headlines are visible without the user doing anything.
        self.assertIn("Nifty rallies on FII buying", out["html"])
        self.assertIn("RBI keeps repo rate unchanged", out["html"])
        # Bot language, and the blocked NSE source is stated honestly.
        self.assertIn("Bot ne abhi check kiya", out["html"])
        self.assertIn("NSE announcements unavailable", out["html"])

    def test_home_news_failure_is_honest(self):
        from unittest.mock import patch
        from web import app as app_mod
        app_mod._cache.clear()
        with patch.object(news_mod, "check_symbol", side_effect=RuntimeError("network down")):
            out = app_mod._api_news({}, "hi")
        self.assertIn("News unavailable", out["html"])
        self.assertNotIn("Nifty rallies", out["html"])


def _structure_bars():
    """Crafted 5m bars: a downtrend, then a bullish CHoCH, then a BOS."""
    base = datetime(2026, 11, 26, 9, 15, tzinfo=IST)
    rows = [
        # (minutes, open, high, low, close, volume)
        (0, 100, 101, 99, 100, 1000),
        (5, 100, 102, 100, 101, 1000),
        (10, 101, 103, 100, 102, 1000),   # swing high 103
        (15, 102, 102, 99, 100, 1000),
        (20, 100, 100, 97, 98, 1000),     # swing low 97
        (25, 98, 99, 97.5, 99, 1000),
        (30, 99, 101, 98, 100, 1000),
        (35, 100, 104, 100, 104, 1000),   # close 104 > 103 -> CHoCH
        (40, 104, 105, 103, 105, 1000),   # swing high 105
        (45, 105, 104, 104, 104, 1000),
        (50, 104, 104, 103, 103, 1000),
        (55, 103, 106, 103, 106, 1000),   # close 106 > 105 -> BOS
        (60, 106, 106, 105, 105, 1000),
    ]
    idx = pd.DatetimeIndex(
        [base + pd.Timedelta(minutes=m) for m, *_ in rows], tz=IST
    )
    return pd.DataFrame(
        {
            "open": [r[1] for r in rows],
            "high": [r[2] for r in rows],
            "low": [r[3] for r in rows],
            "close": [r[4] for r in rows],
            "volume": [r[5] for r in rows],
        },
        index=idx,
    )


def _fvg_bars(filled: bool = False):
    """Bars with a bullish fair value gap (bar0.high < bar2.low)."""
    base = datetime(2026, 11, 26, 9, 15, tzinfo=IST)
    rows = [
        (0, 100, 100, 99, 99.5, 1000),
        (5, 99.5, 101, 99, 100, 1000),
        (10, 102, 103, 102, 102.5, 1000),  # gap [100, 102]
        (15, 102.5, 104, 103, 103.5, 1000),
        (20, 103.5, 105, 103, 104, 1000),
    ]
    if filled:
        rows.append((25, 104, 104, 101, 102, 1000))  # low 101 retraces into the gap
    idx = pd.DatetimeIndex(
        [base + pd.Timedelta(minutes=m) for m, *_ in rows], tz=IST
    )
    return pd.DataFrame(
        {
            "open": [r[1] for r in rows],
            "high": [r[2] for r in rows],
            "low": [r[3] for r in rows],
            "close": [r[4] for r in rows],
            "volume": [r[5] for r in rows],
        },
        index=idx,
    )


class TestStructure(unittest.TestCase):
    def test_vwap_equal_volume_is_mean_typical(self):
        bars = pd.DataFrame(
            {
                "open": [100, 102, 104],
                "high": [102, 104, 106],
                "low": [98, 100, 102],
                "close": [100, 102, 104],
                "volume": [1000, 1000, 1000],
            },
            index=pd.DatetimeIndex(
                ["2026-11-26 09:15", "2026-11-26 09:20", "2026-11-26 09:25"], tz=IST
            ),
        )
        vw = structure_mod.vwap(bars)
        self.assertAlmostEqual(vw.iloc[0], 100.0)
        self.assertAlmostEqual(vw.iloc[-1], 102.0)

    def test_vwap_no_volume_is_nan_not_crash(self):
        # Index bars carry no volume — VWAP is undefined (NaN), never an error.
        bars = pd.DataFrame(
            {
                "open": [100, 102, 104],
                "high": [102, 104, 106],
                "low": [98, 100, 102],
                "close": [100, 102, 104],
                "volume": [None, None, None],
            },
            index=pd.DatetimeIndex(
                ["2026-11-26 09:15", "2026-11-26 09:20", "2026-11-26 09:25"], tz=IST
            ),
        )
        vw = structure_mod.vwap(bars)
        self.assertTrue(vw.isna().all())

    def test_swing_points_in_time_order(self):
        pts = structure_mod.swing_points(_structure_bars())
        self.assertEqual([(p["kind"], p["price"]) for p in pts],
                         [("high", 103.0), ("low", 97.0), ("high", 105.0)])
        times = [p["time"] for p in pts]
        self.assertEqual(times, sorted(times))

    def test_structure_breaks_choch_then_bos(self):
        breaks = structure_mod.structure_breaks(_structure_bars())
        self.assertEqual(len(breaks), 2)
        self.assertEqual(breaks[0]["kind"], "CHoCH")
        self.assertEqual(breaks[0]["direction"], "bullish")
        self.assertEqual(breaks[0]["price"], 104.0)
        self.assertEqual(breaks[1]["kind"], "BOS")
        self.assertEqual(breaks[1]["direction"], "bullish")
        self.assertEqual(breaks[1]["price"], 106.0)

    def test_structure_breaks_empty_bars(self):
        empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        self.assertEqual(structure_mod.structure_breaks(empty), [])
        self.assertEqual(structure_mod.swing_points(empty), [])

    def test_order_blocks_last_counter_trend_candle(self):
        bars = _structure_bars()
        obs = structure_mod.order_blocks(bars)
        # Most recent first: BOS break -> bar10 [103, 104]; CHoCH -> bar4 [97, 100].
        self.assertEqual(len(obs), 2)
        self.assertEqual(obs[0]["kind"], "bullish")
        self.assertEqual(obs[0]["top"], 104.0)
        self.assertEqual(obs[0]["bottom"], 103.0)
        self.assertEqual(obs[1]["top"], 100.0)
        self.assertEqual(obs[1]["bottom"], 97.0)

    def test_fvg_unfilled_found(self):
        fvgs = structure_mod.fair_value_gaps(_fvg_bars(filled=False))
        self.assertEqual(len(fvgs), 1)
        self.assertEqual(fvgs[0]["kind"], "bullish")
        self.assertEqual(fvgs[0]["top"], 102.0)
        self.assertEqual(fvgs[0]["bottom"], 100.0)

    def test_fvg_filled_dropped(self):
        self.assertEqual(structure_mod.fair_value_gaps(_fvg_bars(filled=True)), [])


class TestCharts(unittest.TestCase):
    def test_bars_to_candles(self):
        candles = charts_mod.bars_to_candles(_bars_5m())
        self.assertEqual(len(candles), 6)
        self.assertEqual(candles[0]["open"], 25700.0)
        self.assertEqual(candles[-1]["close"], 25738.0)
        times = [c["time"] for c in candles]
        self.assertEqual(times, sorted(times))
        self.assertTrue(all(isinstance(t, int) for t in times))

    def test_bars_to_line_and_pct(self):
        line = charts_mod.bars_to_line(_bars_5m())
        self.assertEqual([p["value"] for p in line],
                         [25710.0, 25705.0, 25708.0, 25718.0, 25730.0, 25738.0])
        pct = charts_mod.bars_to_pct_line(_bars_5m())
        self.assertEqual(pct[0]["value"], 0.0)
        self.assertAlmostEqual(pct[-1]["value"], (25738 / 25710 - 1) * 100, places=2)

    def test_intraday_series_offline(self):
        from unittest.mock import patch
        with patch.object(charts_mod, "fetch_intraday_bars_meta",
                          return_value=(_bars_5m(), False, "")):
            payload = charts_mod.intraday_series("^NSEI")
        self.assertEqual(len(payload["candles"]), 6)
        self.assertEqual(len(payload["line"]), 6)
        self.assertIsNone(payload["error"])
        self.assertFalse(payload["stale"])

    def test_intraday_series_error_is_honest(self):
        from unittest.mock import patch
        with patch.object(
            charts_mod, "fetch_intraday_bars_meta",
            side_effect=data_mod.DataError("Yahoo HTTP 403"),
        ):
            payload = charts_mod.intraday_series("^NSEI")
        self.assertEqual(payload["candles"], [])
        self.assertIn("403", payload["error"])

    def test_flow_series_offline(self):
        from unittest.mock import patch
        with patch.object(charts_mod, "fetch_intraday_bars", return_value=_bars_5m()):
            flow = charts_mod.flow_series()
        self.assertEqual(len(flow["series"]), len(data_mod.UNIVERSE))
        self.assertEqual(flow["failed"], [])
        nifty = next(s for s in flow["series"] if s["symbol"] == "NIFTY")
        self.assertTrue(nifty["is_index"])
        self.assertAlmostEqual(nifty["change_pct"], (25738 / 25710 - 1) * 100, places=2)
        self.assertGreater(nifty["traded_value"], 0)

    def test_sector_map_from_flow(self):
        from unittest.mock import patch
        with patch.object(charts_mod, "fetch_intraday_bars", return_value=_bars_5m()):
            sectors = charts_mod.sector_map(charts_mod.flow_series())
        self.assertEqual(len(sectors["sectors"]), len(charts_mod.SECTORS))
        banks = next(s for s in sectors["sectors"] if s["name"] == "Banks")
        self.assertEqual(banks["stocks"], 5)
        self.assertAlmostEqual(banks["change_pct"], (25738 / 25710 - 1) * 100, places=2)
        self.assertAlmostEqual(banks["rel_pct"], 0.0, places=2)
        self.assertGreater(banks["traded_value"], 0)

    def test_orb_chart_offline(self):
        from unittest.mock import patch
        with patch.object(charts_mod, "fetch_intraday_bars", return_value=_bars_5m()), \
             patch.object(levels_mod, "fetch_daily_bars", return_value=_levels_daily_bars()):
            payload = charts_mod.orb_chart("^NSEI", "NIFTY")
        self.assertEqual(payload["signal"], "long")
        self.assertEqual(payload["entry"], 25730.0)
        self.assertEqual(payload["stop"], 25702.5)
        self.assertEqual(payload["target"], 25785.0)
        self.assertEqual(len(payload["candles"]), 6)
        self.assertTrue(len(payload["vwap"]) > 0)
        labels = {ln["label"] for ln in payload["lines"]}
        for want in ("ORB HIGH", "ORB LOW", "PDH", "PDL", "ENTRY", "SL", "TP"):
            self.assertIn(want, labels)
        box_labels = {b["label"] for b in payload["boxes"]}
        self.assertIn("TARGET", box_labels)
        self.assertIn("STOP", box_labels)
        self.assertIsNone(payload["error"])

    def test_levels_chart_offline(self):
        from unittest.mock import patch
        row = {
            "symbol": "NIFTY", "price": 25748.0,
            "nearest_support": 25650.0, "nearest_resistance": 25800.0,
            "pdh": 25800.0, "pdl": 25550.0,
            "orb_high": 25718.0, "orb_low": 25687.0,
            "signal": "long", "entry": 25730.0, "stop": 25702.5,
            "atr_stop": 25690.0, "target": 25785.0, "atr": 10.0,
        }
        with patch.object(levels_mod, "compute_levels", return_value=row), \
             patch.object(charts_mod, "fetch_intraday_bars", return_value=_bars_5m()):
            payload = charts_mod.levels_chart("^NSEI", "NIFTY")
        self.assertEqual(len(payload["candles"]), 6)
        labels = {ln["label"] for ln in payload["lines"]}
        for want in ("SUPPORT", "RESIST", "PDH", "PDL", "ORB HIGH", "ORB LOW", "ENTRY", "SL", "TP"):
            self.assertIn(want, labels)
        self.assertEqual(payload["levels"]["symbol"], "NIFTY")
        self.assertIsNone(payload["error"])

    def test_mc_histogram_binning(self):
        hist = charts_mod.mc_histogram([-1.0, -0.5, 0.2, 1.5, 2.0, 2.0], 0.2)
        self.assertEqual(hist["n"], 6)
        self.assertEqual([b["count"] for b in hist["buckets"]], [1, 1, 1, 1, 2])
        self.assertEqual(hist["buckets"][0]["label"], "Stop (-1R)")
        self.assertEqual(hist["buckets"][4]["label"], "Target (+2R)")
        self.assertEqual(hist["avg_r"], 0.2)
        empty = charts_mod.mc_histogram([], None)
        self.assertEqual(empty["n"], 0)
        self.assertTrue(all(b["count"] == 0 for b in empty["buckets"]))

    def test_mc_histogram_float_target_lands_in_target_bucket(self):
        # reward/risk in floats can be 1.9999999… — still a target hit.
        hist = charts_mod.mc_histogram([139.66 / 69.83, -1.0, 0.5], 0.5)
        self.assertEqual([b["count"] for b in hist["buckets"]], [1, 0, 1, 0, 1])


class TestChartPages(unittest.TestCase):
    def setUp(self):
        import os, time
        from web import app as app_mod
        app_mod._cache.clear()
        wl = Path(__file__).resolve().parent.parent / "data" / "watchlist.json"
        if wl.exists():
            old = time.time() - 3600
            os.utime(wl, (old, old))

    def _mock_news(self):
        return {
            "symbol": "NIFTY", "moving": False, "change_pct": 0.39, "tags": [],
            "items": [{"source": "Yahoo headline", "title": "Nifty rallies on FII buying",
                       "link": "https://example.com/1", "published": "26 Nov 2026", "tags": []}],
            "latest": [{"source": "Yahoo headline", "title": "Nifty rallies on FII buying",
                        "link": "https://example.com/1", "published": "26 Nov 2026", "tags": []}],
            "sources": {"nse_announcements": "ok", "yahoo_headlines": "1 item(s)"},
            "quote_error": "",
        }

    def _mock_scan(self):
        return {
            "scan": "orb", "run_at_ist": "2026-11-26 12:00:00 IST",
            "fetched": 39, "usable": 38,
            "hits": [{"symbol": "NIFTY", "yahoo_symbol": "^NSEI", "direction": "long",
                      "close": 25730.0, "bar_time": "09:35",
                      "range_high": 25718.0, "range_low": 25687.0,
                      "volume": 1500.0, "price_x_volume": 38595000.0,
                      "date": "2026-11-26"}],
            "failed": [], "hit_count": 1, "as_of": "2026-11-26 12:00:00 IST",
        }

    def test_chart_json_escapes_script_close(self):
        from web import app as app_mod
        out = app_mod._chart_json({"x": "</script><script>alert(1)</script>"})
        self.assertNotIn("</script>", out)
        self.assertIn("<\\/script>", out)

    def test_home_has_charts_flow_sectors_news_orb(self):
        from unittest.mock import patch
        from web import app as app_mod
        small = {"symbol": "X", "candles": [{"time": 1, "open": 1, "high": 1, "low": 1, "close": 1}],
                 "line": [{"time": 1, "value": 1.0}], "error": None}
        with patch.object(news_mod, "check_symbol", return_value=self._mock_news()), \
             patch.object(charts_mod, "intraday_series", return_value=small), \
             patch.object(data_mod, "fetch_quote", return_value={"price": 25900.0, "prev_close": 25819.25, "change_pct": 0.31}), \
             patch.object(scanner_mod, "scan_orb", return_value=self._mock_scan()):
            html = app_mod.home("hi")
            news = app_mod._api_news({}, "hi")
        for needle in ("chart-nifty", "chart-bank", "chart-sensex",
                       "Smart Money Flow", "flow-chart", "Sector Map", "sector-map",
                       "News Feed", "ORB Hits", "lightweight-charts"):
            self.assertIn(needle, html)
        # The headline itself arrives via /api/news after the page loads.
        self.assertIn("Nifty rallies on FII buying", news["html"])

    def test_orb_page_has_candle_chart(self):
        from unittest.mock import patch
        from web import app as app_mod
        payload = {"symbol": "NIFTY",
                   "candles": [{"time": 1, "open": 1, "high": 1, "low": 1, "close": 1}],
                   "vwap": [], "lines": [], "markers": [], "boxes": [],
                   "error": None, "signal": "long"}
        with patch.object(scanner_mod, "scan_orb", return_value=self._mock_scan()), \
             patch.object(charts_mod, "orb_chart", return_value=payload):
            html = app_mod.orb_page("en", {"symbol": ["NIFTY"]})
        self.assertIn("orb-chart", html)
        self.assertIn("candlestick with overlays", html)
        self.assertIn("NIFTY", html)

    def test_levels_page_has_candle_chart(self):
        from unittest.mock import patch
        from web import app as app_mod
        row = {"symbol": "NIFTY", "price": 25748.0, "nearest_support": 25650.0,
               "nearest_resistance": 25800.0, "pdh": 25800.0, "pdl": 25550.0,
               "entry": 25730.0, "stop": 25702.5, "atr_stop": 25690.0,
               "target": 25785.0, "signal": "long"}
        payload = {"symbol": "NIFTY",
                   "candles": [{"time": 1, "open": 1, "high": 1, "low": 1, "close": 1}],
                   "vwap": [], "lines": [], "markers": [], "boxes": [],
                   "error": None, "levels": row}
        with patch.object(levels_mod, "scan_levels",
                          return_value={"run_at_ist": "2026-11-26 12:00:00 IST",
                                        "levels": [row], "failed": []}), \
             patch.object(charts_mod, "levels_chart", return_value=payload):
            html = app_mod.levels_page("en", {"symbol": ["NIFTY"]})
        self.assertIn("levels-chart", html)
        self.assertIn("candlestick with overlays", html)

    def test_sectors_page_has_map(self):
        from web import app as app_mod
        html = app_mod.sectors_page("en")
        self.assertIn("sector-map", html)
        self.assertIn("Sector Map", html)

    def test_timemachine_page_has_distribution_chart(self):
        from unittest.mock import patch
        from web import app as app_mod
        hist = charts_mod.mc_histogram([-1.0] * 100 + [2.0] * 50 + [0.3] * 20, 0.1)
        data = {
            "setup": {"symbol": "NIFTY", "signal": "long", "entry": 25730.0,
                      "stop": 25702.5, "target": 25785.0, "orb_low": 25687.0,
                      "orb_high": 25718.0, "price": 25748.0, "atr_stop": None},
            "setup_error": None,
            "monte_carlo": {"available": True, "symbol": "NIFTY", "direction": "long",
                            "entry": 25730.0, "stop": 25702.5, "target": 25785.0,
                            "n": 170, "target_first_pct": 29.4, "stop_first_pct": 58.8,
                            "neither_pct": 11.8, "avg_r": 0.1, "reward_risk": 2.0,
                            "best_case_r": 2.0, "worst_case_r": -1.0,
                            "verdict": "coin flip", "edge_pts": -4.0,
                            "break_even_pct": 33.3, "histogram": hist},
            "trade_check": {"available": False, "reason": "no setup"},
            "backtest": {"available": False, "reason": "no data"},
        }
        with patch.object(timecheck_mod, "collect_timemachine", return_value=data):
            # The body arrives via /api/timemachine; the page itself
            # carries the lazy slot.
            out = app_mod._api_timemachine()
            page = app_mod.timemachine_page("en")
        html = out["html"]
        self.assertIn("timemachine-body", page)
        self.assertIn("Monte Carlo distribution", html)
        self.assertIn("Stop (-1R)", html)
        self.assertIn("Target (+2R)", html)
        self.assertIn("100", html)  # stop bucket count

    def test_mc_histogram_svg(self):
        from web import app as app_mod
        hist = charts_mod.mc_histogram([-1.0, 2.0, 2.0], 0.33)
        svg = app_mod._mc_histogram_svg(hist)
        self.assertIn("Stop (-1R)", svg)
        self.assertIn("Target (+2R)", svg)
        self.assertIn("+0.33R", svg)


class TestExitTargets(unittest.TestCase):
    """TP1 / TP2 / TP3 (1R / 2R / 3R) on the exit plan."""

    def test_long_tp1_tp2_tp3(self):
        plan = exit_mod.compute_exit(
            entry=25730.0, range_high=25718.0, range_low=25687.0)
        # risk per unit = 27.50 (entry 25730 - stop 25702.50)
        self.assertAlmostEqual(plan.risk_per_unit, 27.50)
        self.assertAlmostEqual(plan.tp1, 25757.50)  # entry + 1R
        self.assertAlmostEqual(plan.tp2, 25785.00)  # entry + 2R
        self.assertAlmostEqual(plan.tp3, 25812.50)  # entry + 3R
        self.assertAlmostEqual(plan.target, plan.tp2)  # target == TP2

    def test_short_tp1_tp2_tp3(self):
        plan = exit_mod.compute_exit(
            entry=25680.0, range_high=25718.0, range_low=25687.0)
        self.assertEqual(plan.direction, "short")
        self.assertAlmostEqual(plan.risk_per_unit, 22.50)
        self.assertAlmostEqual(plan.tp1, 25657.50)  # entry - 1R
        self.assertAlmostEqual(plan.tp2, 25635.00)  # entry - 2R
        self.assertAlmostEqual(plan.target, plan.tp2)
        self.assertAlmostEqual(plan.tp3, 25612.50)  # entry - 3R

    def test_orb_plan_carries_tp1_tp2_tp3(self):
        plan = entry_mod.plan_orb_entry(_bars_5m(), direction="long",
                                        symbol="NIFTY")
        self.assertIsNotNone(plan)
        self.assertAlmostEqual(plan.tp1, plan.entry + abs(plan.entry - plan.stop))
        self.assertAlmostEqual(plan.tp2, plan.target)
        self.assertAlmostEqual(plan.tp3, plan.entry + 3 * abs(plan.entry - plan.stop))


def _volume_bars(trigger_volume: float, base_volume: float = 1000.0,
                 n: int = 26) -> pd.DataFrame:
    """Synthetic 5m bars: steady volume, then one trigger bar."""
    base = datetime(2026, 11, 26, 9, 15, tzinfo=IST)
    idx = pd.DatetimeIndex(
        [base + pd.Timedelta(minutes=5 * i) for i in range(n)], tz=IST)
    volumes = [base_volume] * (n - 1) + [trigger_volume]
    closes = [100.0 + i * 0.1 for i in range(n)]
    return pd.DataFrame(
        {"open": closes, "high": [c + 0.2 for c in closes],
         "low": [c - 0.2 for c in closes], "close": closes,
         "volume": volumes},
        index=idx,
    )


class TestVolume(unittest.TestCase):
    def test_high_relative_volume_is_strong(self):
        out = volume_mod.relative_volume(_volume_bars(2500.0))
        self.assertTrue(out["available"])
        self.assertAlmostEqual(out["rel_vol"], 2.5)
        self.assertEqual(out["verdict"], "STRONG")

    def test_low_relative_volume_is_weak(self):
        out = volume_mod.relative_volume(_volume_bars(500.0))
        self.assertAlmostEqual(out["rel_vol"], 0.5)
        self.assertEqual(out["verdict"], "WEAK")

    def test_average_volume_is_normal(self):
        out = volume_mod.relative_volume(_volume_bars(1100.0))
        self.assertAlmostEqual(out["rel_vol"], 1.1)
        self.assertEqual(out["verdict"], "normal")

    def test_trigger_bar_by_time(self):
        bars = _volume_bars(2500.0)
        # The 10:00 bar (index 9) has base volume 1000 -> 1.0x normal.
        out = volume_mod.relative_volume(bars, trigger_time="10:00")
        self.assertAlmostEqual(out["rel_vol"], 1.0)
        self.assertEqual(out["verdict"], "normal")

    def test_volume_trend_rising_and_falling(self):
        base = datetime(2026, 11, 26, 9, 15, tzinfo=IST)
        idx = pd.DatetimeIndex(
            [base + pd.Timedelta(minutes=5 * i) for i in range(10)], tz=IST)
        rising = pd.DataFrame(
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0,
             "volume": [100.0] * 5 + [300.0] * 5}, index=idx)
        self.assertEqual(volume_mod.volume_trend(rising)["trend"], "rising")
        falling = pd.DataFrame(
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0,
             "volume": [300.0] * 5 + [100.0] * 5}, index=idx)
        self.assertEqual(volume_mod.volume_trend(falling)["trend"], "falling")

    def test_analyse_volume_uses_own_bars(self):
        out = volume_mod.analyse_volume("RELIANCE", bars=_volume_bars(2200.0))
        self.assertEqual(out["verdict"], "STRONG")
        self.assertAlmostEqual(out["rel_vol"], 2.2)

    def test_analyse_volume_never_blocks_when_nothing_fetches(self):
        from unittest.mock import patch
        with patch.object(volume_mod, "_fetch_proxy_bars",
                          return_value=pd.DataFrame()):
            out = volume_mod.analyse_volume("NIFTY", bars=pd.DataFrame())
        # A verdict is always returned, neutral, and the wording never
        # says the data is missing.
        self.assertEqual(out["verdict"], "normal")
        self.assertIsNone(out["rel_vol"])
        self.assertNotIn("unavailable", out["reason"].lower())
        self.assertNotIn("proxy", out["reason"].lower())


def _news_result(*titles: str) -> dict:
    return {"latest": [
        {"source": "Yahoo headline", "title": t, "link": "",
         "published": "", "tags": []} for t in titles]}


_MC_GOOD = {"available": True, "target_first_pct": 40.0,
            "stop_first_pct": 50.0, "neither_pct": 10.0, "avg_r": 0.2,
            "n": 10000, "break_even_pct": 33.3}
_MC_HALF = {"available": True, "target_first_pct": 30.0,
            "stop_first_pct": 58.0, "neither_pct": 12.0, "avg_r": -0.1,
            "n": 10000, "break_even_pct": 33.3}
_MC_BAD = {"available": True, "target_first_pct": 18.0,
           "stop_first_pct": 70.0, "neither_pct": 12.0, "avg_r": -0.4,
           "n": 10000, "break_even_pct": 33.3}
_VOL_STRONG = {"symbol": "NIFTY", "rel_vol": 2.1, "verdict": "STRONG",
               "trend": "rising", "reason": "Trigger bar volume 2.1x."}
_VOL_WEAK = {"symbol": "NIFTY", "rel_vol": 0.5, "verdict": "WEAK",
             "trend": "falling", "reason": "Trigger bar volume 0.5x."}
_VOL_NORMAL = {"symbol": "NIFTY", "rel_vol": 1.1, "verdict": "normal",
               "trend": "flat", "reason": "Trigger bar volume 1.1x."}


class TestSignal(unittest.TestCase):
    def _signal(self, **kwargs):
        base = dict(symbol="NIFTY", direction="long", entry=25730.0,
                    stop=25702.5, target=25785.0, support=25687.0,
                    resistance=25800.0, news_result=_news_result(
                        "Reliance Q2 profit rises 12% to a record"),
                    mc_result=_MC_GOOD, volume_result=_VOL_STRONG)
        base.update(kwargs)
        return signal_mod.entry_signal(**base)

    def test_clean_setup_enters(self):
        sig = self._signal()
        self.assertEqual(sig["decision"], "ENTER")
        self.assertEqual(sig["size_factor"], 1.0)
        self.assertAlmostEqual(sig["tp1"], 25757.50)
        self.assertAlmostEqual(sig["tp2"], 25785.00)
        self.assertAlmostEqual(sig["tp3"], 25812.50)

    def test_negative_news_blocks_entry(self):
        sig = self._signal(news_result=_news_result(
            "SEBI probe: company faces fraud penalty, shares fall"))
        self.assertEqual(sig["decision"], "BLOCKED")
        self.assertEqual(sig["size_factor"], 0.0)
        self.assertEqual(sig["factors"]["news"]["verdict"], "block")
        self.assertIn("fraud", sig["factors"]["news"]["reason"])

    def test_positive_news_supports_entry(self):
        sig = self._signal()
        self.assertEqual(sig["factors"]["news"]["verdict"], "support")
        self.assertEqual(sig["decision"], "ENTER")

    def test_positive_news_blocks_a_short(self):
        sig = self._signal(direction="short", entry=25680.0, stop=25702.5,
                           target=25635.0, support=25650.0,
                           resistance=25718.0)
        self.assertEqual(sig["factors"]["news"]["verdict"], "block")
        self.assertEqual(sig["decision"], "BLOCKED")

    def test_negative_news_supports_a_short(self):
        sig = self._signal(direction="short", entry=25680.0, stop=25702.5,
                           target=25635.0, support=25650.0,
                           resistance=25718.0,
                           news_result=_news_result(
                               "Company reports loss, shares crash"))
        self.assertEqual(sig["factors"]["news"]["verdict"], "support")
        self.assertEqual(sig["decision"], "ENTER")

    def test_bad_monte_carlo_blocks_entry(self):
        sig = self._signal(mc_result=_MC_BAD)
        self.assertEqual(sig["decision"], "BLOCKED")
        self.assertEqual(sig["factors"]["mc"]["verdict"], "block")
        self.assertAlmostEqual(
            sig["factors"]["mc"]["target_first_pct"], 18.0)

    def test_mediocre_monte_carlo_halves_size(self):
        sig = self._signal(mc_result=_MC_HALF)
        self.assertEqual(sig["decision"], "HALF SIZE")
        self.assertEqual(sig["size_factor"], 0.5)
        self.assertEqual(sig["factors"]["mc"]["verdict"], "half")

    def test_weak_volume_halves_size(self):
        sig = self._signal(volume_result=_VOL_WEAK)
        self.assertEqual(sig["decision"], "HALF SIZE")
        self.assertEqual(sig["size_factor"], 0.5)
        self.assertEqual(sig["factors"]["volume"]["verdict"], "half")
        self.assertEqual(sig["factors"]["volume"]["volume_verdict"], "WEAK")

    def test_resistance_before_tp1_halves_size(self):
        sig = self._signal(resistance=25740.0)
        self.assertEqual(sig["decision"], "HALF SIZE")
        self.assertEqual(sig["factors"]["levels"]["verdict"], "half")

    def test_no_setup_is_no_signal(self):
        sig = signal_mod.entry_signal("NIFTY", "long", None, None, None)
        self.assertEqual(sig["decision"], "NO SIGNAL")

    def test_news_unavailable_is_price_only_not_a_block(self):
        from unittest.mock import patch
        with patch.object(news_mod, "check_symbol",
                          side_effect=RuntimeError("network down")):
            factor = signal_mod.news_factor("NIFTY", "long")
        self.assertFalse(factor["available"])
        self.assertEqual(factor["verdict"], "neutral")
        self.assertIn("price-only", factor["reason"])
        with patch.object(news_mod, "check_symbol",
                          side_effect=RuntimeError("network down")):
            sig = signal_mod.entry_signal(
                "NIFTY", "long", 25730.0, 25702.5, 25785.0,
                support=25687.0, resistance=25800.0,
                mc_result=_MC_GOOD, volume_result=_VOL_STRONG)
        self.assertEqual(sig["decision"], "ENTER")
        self.assertIn("price-only", sig["factors"]["news"]["reason"])

    def test_fresh_negative_news_on_open_long_exits_now(self):
        out = signal_mod.exit_signal(
            "NIFTY", "long", 25730.0,
            news_result=_news_result(
                "SEBI probe: company faces fraud penalty, shares fall"))
        self.assertEqual(out["action"], "EXIT NOW")
        self.assertIn("fraud", out["reason"])

    def test_mild_negative_news_on_open_long_exits_early(self):
        out = signal_mod.exit_signal(
            "NIFTY", "long", 25730.0,
            news_result=_news_result("Company shares fall after a soft quarter"))
        self.assertEqual(out["action"], "EXIT EARLY")

    def test_no_news_against_open_position_holds(self):
        out = signal_mod.exit_signal(
            "NIFTY", "long", 25730.0,
            news_result=_news_result("Reliance Q2 profit rises 12%"))
        self.assertEqual(out["action"], "HOLD")

    def test_positive_news_on_open_short_exits(self):
        out = signal_mod.exit_signal(
            "NIFTY", "short", 25680.0,
            news_result=_news_result(
                "Company profit surges to a record high"))
        self.assertIn(out["action"], ("EXIT NOW", "EXIT EARLY"))


class TestSignalDashboard(unittest.TestCase):
    def setUp(self):
        import os, time
        from web import app as app_mod
        app_mod._cache.clear()
        wl = Path(__file__).resolve().parent.parent / "data" / "watchlist.json"
        if wl.exists():
            old = time.time() - 3600
            os.utime(wl, (old, old))

    def _mock_scan(self):
        return {
            "scan": "orb", "run_at_ist": "2026-11-26 12:00:00 IST",
            "fetched": 39, "usable": 38,
            "hits": [{"symbol": "NIFTY", "yahoo_symbol": "^NSEI",
                      "direction": "long", "close": 25730.0,
                      "bar_time": "09:35", "range_high": 25718.0,
                      "range_low": 25687.0, "volume": 1500.0,
                      "price_x_volume": 38595000.0, "date": "2026-11-26"}],
            "failed": [], "hit_count": 1, "as_of": "2026-11-26 12:00:00 IST",
        }

    def _mock_news(self):
        return {
            "symbol": "NIFTY", "moving": False, "change_pct": 0.39,
            "tags": [], "items": [], "latest": [
                {"source": "Yahoo headline",
                 "title": "Reliance Q2 profit rises 12% to a record",
                 "link": "", "published": "", "tags": ["earnings"]}],
            "sources": {"nse_announcements": "ok",
                        "yahoo_headlines": "1 item(s)"},
            "quote_error": "",
        }

    def _mock_mc(self):
        return {
            "setup": {"symbol": "NIFTY", "signal": "long",
                      "entry": 25730.0, "stop": 25702.5, "target": 25785.0},
            "setup_error": None,
            "monte_carlo": dict(_MC_GOOD, symbol="NIFTY",
                                direction="long", entry=25730.0,
                                stop=25702.5, target=25785.0),
            "trade_check": {"available": False, "reason": "no setup"},
            "backtest": {"available": False, "reason": "no data"},
        }

    def test_orb_breakdown_api_shows_signal(self):
        # /orb itself renders instantly (lazy placeholders); the
        # breakdown arrives via /api/orbbreakdown and the table via
        # /api/orb — test those fragments directly.
        from unittest.mock import patch
        from web import app as app_mod
        with patch.object(scanner_mod, "scan_orb",
                          return_value=self._mock_scan()), \
             patch.object(news_mod, "check_symbol",
                          return_value=self._mock_news()), \
             patch.object(timecheck_mod, "collect_timemachine",
                          return_value=self._mock_mc()), \
             patch.object(volume_mod, "analyse_volume",
                          return_value=dict(_VOL_STRONG)), \
             patch.object(data_mod, "fetch_intraday_bars",
                          return_value=_volume_bars(2500.0)):
            out = app_mod._api_orbbreakdown({"symbol": ["NIFTY"]}, "en")
            table = app_mod._api_orb({}, "en")
        html = out["html"]
        for needle in ("Signal breakdown", "TP1", "TP2", "TP3",
                       "25,757.50", "25,785.00", "25,812.50",
                       "Volume:", "STRONG", "News:", "Time Machine:"):
            self.assertIn(needle, html)
        for needle in ("TP1 / TP2 / TP3", "25,757.50", "25,785.00",
                       "25,812.50"):
            self.assertIn(needle, table["html"])
        # The page itself carries the lazy placeholder + loader.
        page = app_mod.orb_page("en", {"symbol": ["NIFTY"]})
        self.assertIn("orb-breakdown", page)
        self.assertIn("Signal breakdown load ho raha hai", page)

    def test_levels_breakdown_api_shows_signal(self):
        from unittest.mock import patch
        from web import app as app_mod
        row = {"symbol": "NIFTY", "price": 25748.0,
               "nearest_support": 25650.0, "nearest_resistance": 25800.0,
               "pdh": 25800.0, "pdl": 25550.0, "entry": 25730.0,
               "stop": 25702.5, "atr_stop": 25690.0, "target": 25785.0,
               "tp1": 25757.5, "tp2": 25785.0, "tp3": 25812.5,
               "signal": "long"}
        with patch.object(levels_mod, "scan_levels",
                          return_value={"run_at_ist": "2026-11-26 12:00:00 IST",
                                        "levels": [row], "failed": []}), \
             patch.object(news_mod, "check_symbol",
                          return_value=self._mock_news()), \
             patch.object(timecheck_mod, "collect_timemachine",
                          return_value=self._mock_mc()), \
             patch.object(volume_mod, "analyse_volume",
                          return_value=dict(_VOL_STRONG)), \
             patch.object(data_mod, "fetch_intraday_bars",
                          return_value=_volume_bars(2500.0)):
            out = app_mod._api_levelsbreakdown({"symbol": ["NIFTY"]}, "en")
            table = app_mod._api_levels({}, "en")
        html = out["html"]
        for needle in ("Signal breakdown", "TP1", "TP2", "TP3",
                       "25,757.50", "25,785.00", "25,812.50",
                       "Volume:", "STRONG", "News:", "Time Machine:"):
            self.assertIn(needle, html)
        for needle in ("TP1", "TP2", "TP3",
                       "25,757.50", "25,785.00", "25,812.50"):
            self.assertIn(needle, table["html"])
        page = app_mod.levels_page("en", {"symbol": ["NIFTY"]})
        self.assertIn("levels-breakdown", page)
        self.assertIn("Signal breakdown load ho raha hai", page)

    def test_paper_page_shows_exit_check_on_bad_news(self):
        from unittest.mock import patch
        from web import app as app_mod
        from datetime import date
        positions = [{"id": "P-0001", "symbol": "NIFTY",
                      "direction": "long", "qty": 1, "entry": 25730.0,
                      "stop": 25702.5, "target": 25785.0}]
        bad_news = _news_result(
            "SEBI probe: company faces fraud penalty, shares fall")
        with patch.object(paper_mod, "list_positions",
                          return_value=positions), \
             patch.object(paper_mod, "list_trades", return_value=[]), \
             patch.object(paper_mod, "day_summary",
                          return_value={"trades": 0, "realised_pnl": 0.0,
                                        "risk_taken": 0.0, "flat": True}), \
             patch.object(news_mod, "check_symbol", return_value=bad_news):
            html = app_mod.paper_page("en")
        self.assertIn("EXIT NOW", html)
        self.assertIn("P-0001", html)


class TestLiveRefresh(unittest.TestCase):
    """60-second auto-refresh: timer, LIVE indicator, failure honesty."""

    def test_refresh_interval_is_60s(self):
        from web import app as app_mod
        self.assertEqual(app_mod.LIVE_REFRESH_MS, 60_000)
        js = app_mod._live_js()
        self.assertIn("60000", js)
        self.assertIn("BHANU_REFRESH_MS", js)

    def test_live_indicator_on_chart_pages_only(self):
        from web import app as app_mod
        for html in (app_mod.home("en"), app_mod.orb_page("en", {"symbol": ["NIFTY"]}),
                     app_mod.levels_page("en", {"symbol": ["NIFTY"]}),
                     app_mod.sectors_page("en"), app_mod.timemachine_page("en")):
            self.assertIn("bhanu-live-indicator", html)
            self.assertIn("LIVE", html)
        for html in (app_mod.chain_page("en", {"symbol": ["NIFTY"]}),
                     app_mod.sizing_page("en", {}), app_mod.paper_page("en"),
                     app_mod.skills_page("en")):
            self.assertNotIn("bhanu-live-indicator", html)

    def test_nav_matches_screenshot(self):
        """Top nav stays exactly as the screenshot: plain blue
        underlined links with " | " separators, in this order — never
        buttons, pills or a dark navbar. Blue + readable in both
        themes (WCAG AA contrast)."""
        import re
        from web import app as app_mod
        want = ["home", "ORB Scanner", "Levels", "Sectors", "Option Chain",
                "Sizing", "Paper Trading", "Skills", "Time Machine",
                "EN", "HI"]
        for lang in ("en", "hi"):
            html = app_mod.home(lang)
            nav = re.search(r"<nav>(.*?)</nav>", html, re.S).group(1)
            labels = re.findall(r"<a[^>]*>([^<]+)</a>", nav)
            self.assertEqual(labels, want)
            self.assertEqual(nav.count(" | "), 9)
        page = app_mod._page("en", "x", "")
        css = re.search(r"<style>(.*?)</style>", page, re.S).group(1)
        self.assertIn("nav a{color:var(--accent)}", css)
        self.assertIn("--accent:#2962ff", css)   # light theme blue
        self.assertIn("--accent:#7aa2ff", css)   # dark theme blue
        # No button/pill styling on the nav links, and no rule that
        # would remove their underline.
        self.assertNotIn("text-decoration:none", css)

        def _lum(hexs):
            r, g, b = (int(hexs[i:i + 2], 16) / 255 for i in (1, 3, 5))
            f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
            r, g, b = f(r), f(g), f(b)
            return 0.2126 * r + 0.7152 * g + 0.0722 * b

        def _contrast(fg, bg):
            l1, l2 = sorted((_lum(fg), _lum(bg)), reverse=True)
            return (l1 + 0.05) / (l2 + 0.05)

        self.assertGreaterEqual(_contrast("#2962ff", "#f7f8fa"), 4.5)
        self.assertGreaterEqual(_contrast("#7aa2ff", "#121417"), 4.5)

    def test_live_js_handles_hidden_tab_failure_and_retry(self):
        from web import app as app_mod
        js = app_mod._live_js()
        for needle in ("document.hidden", "allSettled", "update failed, retrying",
                       "last good", "visibilitychange", "bhanuMarkLive"):
            self.assertIn(needle, js)

    def test_chart_pages_register_refreshers_and_mark_live(self):
        from web import app as app_mod
        home = app_mod.home("en")
        for needle in ("/api/intraday", "/api/quotes", "/api/news",
                       "bhanuQuoteRefresh", "bhanuNewsRefresh",
                       "bhanuLazyLine", "bhanuFlowChart", "bhanuSectorMap",
                       "bhanuOrbHitsFetch"):
            self.assertIn(needle, home)
        orb = app_mod.orb_page("en", {"symbol": ["NIFTY"]})
        self.assertIn("/api/orbchart", orb)
        self.assertIn("bhanuOrbLoad", orb)
        levels = app_mod.levels_page("en", {"symbol": ["NIFTY"]})
        self.assertIn("/api/levelschart", levels)
        self.assertIn("bhanuLevelsLoad", levels)
        tm = app_mod.timemachine_page("en")
        self.assertIn("/api/timemachine", tm)
        self.assertIn("bhanuTimemachineLoad", tm)

    def test_no_market_lab_name_in_live_or_chart_js(self):
        from web import app as app_mod
        self.assertNotIn("Market Lab", app_mod._live_js())
        self.assertNotIn("Market Lab", app_mod._chart_js())

    def test_home_line_chart_calls_come_after_definition(self):
        """Regression: the 3 home intraday charts were blank because
        ``bhanuLazyLine`` was called from an inline <script> inside
        each card body — which runs during parsing, BEFORE ``_chart_js``
        defines the function, so it threw ReferenceError and the fetch
        never started. The calls must live in the scripts section,
        after the definition, and the card must carry no inline script."""
        from web import app as app_mod
        html = app_mod.home("en")
        definition = html.find("function bhanuLazyLine")
        self.assertGreater(definition, 0)
        for cid, symbol in (("chart-nifty", "^NSEI"),
                            ("chart-bank", "^NSEBANK"),
                            ("chart-sensex", "^BSESN")):
            call = html.find(f"bhanuLazyLine('{cid}', '{symbol}')")
            self.assertGreater(call, 0, f"missing lazy call for {cid}")
            self.assertGreater(call, definition,
                               f"call for {cid} runs before the function is defined")
        block = app_mod._line_chart_block("chart-nifty", "NIFTY 50", "^NSEI")
        self.assertNotIn("<script>", block)
        self.assertIn("Chart load ho raha hai", block)

    def test_lazy_line_fetch_has_timeout(self):
        """A hung intraday fetch must fail (so the retry loop runs)
        instead of leaving "loading" forever."""
        from web import app as app_mod
        js = app_mod._chart_js()
        self.assertIn("AbortController", js)
        self.assertIn("bhanuFetchJson", js)
        self.assertIn("request timed out", js)

    def test_api_intraday_failure_is_honest(self):
        from unittest.mock import patch
        from web import app as app_mod
        app_mod._cache.clear()
        with patch.object(charts_mod, "intraday_series",
                          return_value={"symbol": "^NSEI", "line": [], "candles": [],
                                        "error": "Yahoo HTTP 403"}):
            out = app_mod._api_intraday({"symbol": ["^NSEI"]})
        self.assertEqual(out["line"], [])
        self.assertIn("403", out["error"])


class TestTableOverflow(unittest.TestCase):
    """Wide tables must never spill out of their card: every table
    sits in a .table-wrap (horizontal scroll inside the box) with a
    fixed layout, and long cell text wraps instead of widening it."""

    def test_table_wrap_css(self):
        from web import app as app_mod
        page = app_mod._page("en", "x", "")
        css = page[page.find("<style>"):page.find("</style>")]
        self.assertIn(".table-wrap{overflow-x:auto", css)
        self.assertIn(".table-wrap>table{table-layout:fixed", css)
        self.assertIn("overflow-wrap:break-word", css)
        self.assertIn("box-sizing:border-box", css)

    def test_levels_table_stays_in_its_box(self):
        from unittest.mock import patch
        from web import app as app_mod
        row = {"symbol": "NIFTY", "price": 25748.0,
               "nearest_support": 25650.0, "nearest_resistance": 25800.0,
               "pdh": 25800.0, "pdl": 25550.0,
               "entry": 25730.0, "stop": 25822.33, "atr_stop": 25793.17,
               "target": 25785.0, "signal": "short"}
        with patch.object(levels_mod, "scan_levels",
                          return_value={"run_at_ist": "2026-11-26 13:00:00 IST",
                                        "levels": [row], "failed": []}):
            out = app_mod._api_levels({}, "en")
        html = out["html"]
        self.assertIn("table-wrap", html)
        self.assertIn("<colgroup>", html)
        # Narrow screens: the table keeps a readable minimum width and
        # scrolls inside its wrapper instead of crushing to letters.
        self.assertIn("min-width:720px", html)
        # The long SL text wraps inside its cell, it does not widen
        # the table: it is present and inside the wrapped table.
        self.assertIn("25,822.33 (1.5x ATR 25,793.17)", html)
        self.assertLess(html.find("table-wrap"), html.find("<table"))

    def test_orb_table_stays_in_its_box(self):
        from unittest.mock import patch
        from web import app as app_mod
        scan = {
            "scan": "orb", "run_at_ist": "2026-11-26 12:00:00 IST",
            "fetched": 39, "usable": 38,
            "hits": [{"symbol": "NIFTY", "yahoo_symbol": "^NSEI",
                      "direction": "long", "close": 25730.0,
                      "bar_time": "09:35", "range_high": 25718.0,
                      "range_low": 25687.0}],
            "failed": [], "hit_count": 1,
            "as_of": "2026-11-26 12:00:00 IST",
        }
        with patch.object(app_mod, "_orb_scan", return_value=scan):
            out = app_mod._api_orb({}, "en")
        self.assertIn("table-wrap", out["html"])
        self.assertIn("<colgroup>", out["html"])
        self.assertIn("min-width:600px", out["html"])
        self.assertLess(out["html"].find("table-wrap"),
                        out["html"].find("<table"))

    def test_paper_tables_stay_in_their_boxes(self):
        from unittest.mock import patch
        from web import app as app_mod
        with patch.object(paper_mod, "list_positions", return_value=[]), \
             patch.object(paper_mod, "list_trades", return_value=[]), \
             patch.object(paper_mod, "day_summary",
                          return_value={"trades": 0, "realised_pnl": 0.0,
                                        "risk_taken": 0.0, "flat": True}):
            html = app_mod.paper_page("en")
        # Open positions + exit check + closed trades: all three wrapped.
        # (Count the divs, not the word — the page CSS also mentions it.)
        self.assertEqual(html.count("class='table-wrap'"), 3)

    def test_chain_and_skills_tables_stay_in_their_boxes(self):
        from unittest.mock import patch
        from web import app as app_mod
        from bhanu import options as options_mod
        chain = {"underlying": 25800.0, "atm": 25800, "pcr": 1.0,
                 "ce_oi": 1000, "pe_oi": 1000,
                 "strikes": [{"strike": 25800,
                              "ce": {"lastPrice": 100.0, "openInterest": 1000},
                              "pe": {"lastPrice": 90.0, "openInterest": 900}}]}
        with patch.object(options_mod, "fetch_chain", return_value=chain):
            html = app_mod.chain_page("en", {"symbol": ["NIFTY"]})
        self.assertIn("table-wrap", html)
        html = app_mod.skills_page("en")
        self.assertIn("table-wrap", html)
        self.assertIn("<colgroup>", html)

    def test_sector_and_mc_tables_wrapped_in_chart_js(self):
        from web import app as app_mod
        js = app_mod._chart_js()
        self.assertIn('class="table-wrap"><table>', js)
        self.assertIn('<div class="table-wrap"><table>', js)


class TestPerformance(unittest.TestCase):
    """Pages render instantly: no server fetch blocks a page load."""

    def setUp(self):
        from web import app as app_mod
        app_mod._cache.clear()

    def test_home_renders_under_2s_with_warm_cache(self):
        import time
        from unittest.mock import patch
        from web import app as app_mod
        with patch.object(paper_mod, "day_summary",
                          return_value={"trades": 0, "realised_pnl": 0.0,
                                        "risk_taken": 0.0, "flat": True}):
            t0 = time.time()
            html = app_mod.home("hi")
            secs = time.time() - t0
        self.assertLess(secs, 2.0)
        self.assertIn("Bhanu Indian Day Trader", html)

    def test_charts_load_async_no_embedded_json(self):
        from web import app as app_mod
        html = app_mod.home("en")
        self.assertIn("bhanuLazyLine", html)
        self.assertIn("/api/intraday?symbol=", html)
        self.assertNotIn("chart-nifty-data", html)
        self.assertNotIn("chart-bank-data", html)
        self.assertNotIn("chart-sensex-data", html)
        orb = app_mod.orb_page("en", {"symbol": ["NIFTY"]})
        self.assertIn("bhanuOrbLoad", orb)
        self.assertNotIn("orb-chart-data", orb)
        levels = app_mod.levels_page("en", {"symbol": ["NIFTY"]})
        self.assertIn("bhanuLevelsLoad", levels)
        self.assertNotIn("levels-chart-data", levels)

    def test_loading_states_on_every_lazy_section(self):
        from web import app as app_mod
        for html in (app_mod.home("en"), app_mod.orb_page("en", {"symbol": ["NIFTY"]}),
                     app_mod.levels_page("en", {"symbol": ["NIFTY"]}),
                     app_mod.timemachine_page("en")):
            self.assertIn("load ho raha hai", html)

    def test_cache_hit_avoids_second_fetch(self):
        from unittest.mock import patch
        from web import app as app_mod
        small = {"symbol": "^NSEI", "line": [{"time": 1, "value": 1.0}],
                 "candles": [], "error": None}
        with patch.object(charts_mod, "intraday_series",
                          return_value=small) as mocked:
            app_mod._api_intraday({"symbol": ["^NSEI"]})
            app_mod._api_intraday({"symbol": ["^NSEI"]})
        self.assertEqual(mocked.call_count, 1)
        with patch.object(data_mod, "fetch_quote",
                          return_value={"price": 1.0, "prev_close": 1.0,
                                        "change_pct": 0.0}) as mocked_q:
            app_mod._api_quotes()
            app_mod._api_quotes()
        self.assertEqual(mocked_q.call_count, 3)  # once per index, then cached

    def test_api_orb_and_levels_return_html_fragments(self):
        from unittest.mock import patch
        from web import app as app_mod
        scan = {
            "scan": "orb", "run_at_ist": "2026-11-26 12:00:00 IST",
            "fetched": 39, "usable": 38,
            "hits": [{"symbol": "NIFTY", "yahoo_symbol": "^NSEI", "direction": "long",
                      "close": 25730.0, "bar_time": "09:35",
                      "range_high": 25718.0, "range_low": 25687.0}],
            "failed": [], "hit_count": 1, "as_of": "2026-11-26 12:00:00 IST",
        }
        with patch.object(app_mod, "_orb_scan", return_value=scan):
            out = app_mod._api_orb({}, "en")
        self.assertIn("NIFTY", out["html"])
        self.assertEqual(out["top_symbol"], "NIFTY")
        row = {"symbol": "NIFTY", "price": 25748.0, "nearest_support": 25650.0,
               "nearest_resistance": 25800.0, "pdh": 25800.0, "pdl": 25550.0,
               "entry": 25730.0, "stop": 25702.5, "atr_stop": None,
               "target": 25785.0, "signal": "long"}
        with patch.object(levels_mod, "scan_levels",
                          return_value={"run_at_ist": "2026-11-26 12:00:00 IST",
                                        "levels": [row], "failed": []}):
            out = app_mod._api_levels({}, "en")
        self.assertIn("NIFTY", out["html"])
        self.assertEqual(out["top_symbol"], "NIFTY")


class TestLocalCharts(unittest.TestCase):
    """Chart library is bundled locally; SVG fallback when it cannot load."""

    def test_local_bundle_exists_and_is_lightweight_charts(self):
        path = Path(__file__).resolve().parent.parent / "web" / "static" / "lightweight-charts.js"
        self.assertTrue(path.exists())
        self.assertGreater(path.stat().st_size, 100_000)
        text = path.read_text(encoding="utf-8", errors="ignore")
        self.assertIn("LightweightCharts", text)
        self.assertIn("4.2.0", text)

    def test_chart_lib_tag_prefers_local_bundle(self):
        from web import app as app_mod
        tag = app_mod._chart_lib_tag()
        self.assertIn("/static/lightweight-charts.js", tag)
        self.assertNotIn("unpkg", tag)

    def test_static_file_serving_blocks_traversal(self):
        from web import app as app_mod
        served = app_mod._static_file("lightweight-charts.js")
        self.assertIsNotNone(served)
        data, ctype = served
        self.assertGreater(len(data), 100_000)
        self.assertIn("javascript", ctype)
        self.assertIsNone(app_mod._static_file("../app.py"))
        self.assertIsNone(app_mod._static_file("other.js"))
        self.assertIsNone(app_mod._static_file(""))

    def test_svg_fallback_present_in_chart_js(self):
        from web import app as app_mod
        js = app_mod._chart_js()
        for needle in ("bhanuSvgLine", "bhanuSvgCandles", "bhanuSvgFlow",
                       "typeof LightweightCharts==='undefined'",
                       "Simple chart view"):
            self.assertIn(needle, js)


class TestIntradayRobustness(unittest.TestCase):
    """The intraday fetch must never block a page for ~90s again:
    10s timeout, retry, and a stale disk cache as fallback."""

    def _yahoo_payload(self, frame):
        ts = [int(t.timestamp()) for t in frame.index]
        return {"chart": {"result": [{
            "timestamp": ts,
            "indicators": {"quote": [{
                "open": [float(v) for v in frame["open"]],
                "high": [float(v) for v in frame["high"]],
                "low": [float(v) for v in frame["low"]],
                "close": [float(v) for v in frame["close"]],
                "volume": [float(v) for v in frame["volume"]],
            }]},
        }]}}

    def test_fetch_timeout_is_10s(self):
        import json as _json
        from unittest.mock import patch
        seen = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return _json.dumps({"ok": True}).encode()

        def fake_urlopen(req, timeout=None):
            seen["timeout"] = timeout
            return FakeResp()

        self.assertEqual(data_mod.FETCH_TIMEOUT_S, 10)
        with patch.object(data_mod.urllib.request, "urlopen",
                          side_effect=fake_urlopen):
            out = data_mod._get_json(
                "https://query1.finance.yahoo.com/v8/finance/chart/X")
        self.assertEqual(out, {"ok": True})
        self.assertEqual(seen["timeout"], 10)

    def test_stale_cache_served_when_live_fetch_fails(self):
        import json as _json
        import tempfile
        from unittest.mock import patch
        from datetime import datetime as _dt
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(data_mod, "CACHE_DIR", Path(tmp)):
                key = "intraday_TESTSYM_5m_1d"
                payload = {"saved_at": "2026-11-25T10:00:00",
                           "data": self._yahoo_payload(_bars_5m())}
                data_mod._write_cache(key, payload["data"])
                # Make the cache look old by rewriting saved_at
                # (yesterday, so it is expired in any timezone).
                path = Path(tmp) / f"{key}.json"
                path.write_text(_json.dumps(payload), encoding="utf-8")
                with patch.object(data_mod, "_get_json",
                                  side_effect=data_mod.DataError("Yahoo HTTP 500")):
                    frame, stale, as_of = data_mod.fetch_intraday_bars_meta(
                        "TESTSYM", interval="5m", days=1)
                self.assertFalse(frame.empty)
                self.assertEqual(len(frame), 6)
                self.assertTrue(stale)
                self.assertEqual(as_of, "2026-11-25T10:00:00")

    def test_no_stale_cache_means_honest_error(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(data_mod, "CACHE_DIR", Path(tmp)):
                with patch.object(data_mod, "_get_json",
                                  side_effect=data_mod.DataError("Yahoo HTTP 500")):
                    with self.assertRaises(data_mod.DataError):
                        data_mod.fetch_intraday_bars("TESTSYM", interval="5m", days=1)
                    payload = charts_mod.intraday_series("TESTSYM")
        self.assertEqual(payload["candles"], [])
        self.assertIn("500", payload["error"])

    def test_intraday_series_marks_stale_bars(self):
        from unittest.mock import patch
        with patch.object(charts_mod, "fetch_intraday_bars_meta",
                          return_value=(_bars_5m(), True, "2026-11-26 10:00:00")):
            payload = charts_mod.intraday_series("^NSEI")
        self.assertEqual(len(payload["candles"]), 6)
        self.assertTrue(payload["stale"])
        self.assertEqual(payload["as_of"], "2026-11-26 10:00:00")


if __name__ == "__main__":
    unittest.main()
