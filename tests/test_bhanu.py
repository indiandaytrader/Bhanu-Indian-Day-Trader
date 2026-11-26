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
from bhanu import poller as poller_mod
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
                       "SMART MONEY", "flow-chart", "SECTOR MAP", "sector-map",
                       "NEWS", "ORB Hits", "lightweight-charts"):
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
        for needle in ("SIGNAL", "TP1", "TP2", "TP3",
                       "25,757.50", "25,785.00", "25,812.50",
                       "Volume —", "STRONG", "News —", "Time Machine —"):
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
        for needle in ("SIGNAL", "TP1", "TP2", "TP3",
                       "25,757.50", "25,785.00", "25,812.50",
                       "Volume —", "STRONG", "News —", "Time Machine —"):
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

    def test_nav_is_professional_tabs(self):
        """Top nav is a segmented tab bar (active tab highlighted),
        in the reference order — never plain blue underlined text
        links (26 Nov 15:24 — Bhanu: unprofessional, 50 times said).
        No bare <a> text links, no " | " separators, no underline.
        Blue + readable in both themes (WCAG AA contrast)."""
        import re
        from web import app as app_mod
        want = ["home", "ORB Scanner", "Levels", "Sectors", "Option Chain",
                "Sizing", "Paper Trading", "Skills", "Time Machine"]
        for lang in ("en", "hi"):
            html = app_mod.home(lang)
            nav = re.search(r"<nav class='seg'>(.*?)</nav>", html, re.S).group(1)
            labels = re.findall(r"<a[^>]*>([^<]+)</a>", nav)
            self.assertEqual(labels, want)
            self.assertNotIn(" | ", nav)
            # The home tab is active on the home page.
            self.assertIn("<a href='/?lang=" + lang + "' class='on'>", nav)
            # EN/HI live outside the tab bar, as small pills.
            self.assertIn("<span class='lang'>", html)
        page = app_mod._page("en", "x", "", active="orb")
        self.assertIn("<a href='/orb?lang=en' class='on'>", page)
        css = re.search(r"<style>(.*?)</style>", page, re.S).group(1)
        self.assertIn("--accent:#2563eb", css)   # light theme blue
        self.assertIn("--accent:#7aa2ff", css)   # dark theme blue
        self.assertIn("nav.seg a{", css)
        self.assertIn("nav.seg a.on", css)
        self.assertIn(".ml-top", css)
        # The old plain-link nav styling is gone.
        self.assertNotIn("nav a{color:var(--accent)}", css)

        def _lum(hexs):
            r, g, b = (int(hexs[i:i + 2], 16) / 255 for i in (1, 3, 5))
            f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
            r, g, b = f(r), f(g), f(b)
            return 0.2126 * r + 0.7152 * g + 0.0722 * b

        def _contrast(fg, bg):
            l1, l2 = sorted((_lum(fg), _lum(bg)), reverse=True)
            return (l1 + 0.05) / (l2 + 0.05)

        self.assertGreaterEqual(_contrast("#2563eb", "#edf0f3"), 4.5)
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
        # /skills renders instantly now — the table arrives lazily
        # from /api/skills (cold live fetch used to take ~12 s).
        html = app_mod.skills_page("en")
        self.assertIn("skills-table", html)
        self.assertIn("bhanuSkillsLoad", html)
        self.assertNotIn("<colgroup>", html)
        with patch.object(skills_mod, "collect_skill_status",
                          return_value=[{"daytrader": "orb", "module": "m",
                                         "does": "d", "shows": "s",
                                         "status": "ok"}]):
            out = app_mod._api_skills()
        self.assertIn("table-wrap", out["html"])
        self.assertIn("<colgroup>", out["html"])

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


class TestPoller(unittest.TestCase):
    """The background live engine: parsers, market-hours gating,
    429 backoff, last-good-value, snapshot, and start/stop."""

    def setUp(self):
        import copy
        self._saved_state = copy.deepcopy(poller_mod.STATE)
        self._saved_started = poller_mod._started

    def tearDown(self):
        import copy
        try:
            poller_mod.stop()
        except Exception:  # noqa: BLE001 - teardown must not fail
            pass
        poller_mod.STATE.clear()
        poller_mod.STATE.update(copy.deepcopy(self._saved_state))
        poller_mod._started = self._saved_started

    def test_not_running_after_import(self):
        self.assertFalse(poller_mod.is_running())
        self.assertFalse(poller_mod.status()["running"])

    def test_is_open(self):
        # Thu 26 Nov 2026, 10:00 IST — open; Sat 28 Nov — closed.
        self.assertTrue(poller_mod.is_open(
            datetime(2026, 11, 26, 10, 0, tzinfo=IST)))
        self.assertTrue(poller_mod.is_open(
            datetime(2026, 11, 26, 9, 15, tzinfo=IST)))
        self.assertFalse(poller_mod.is_open(
            datetime(2026, 11, 26, 9, 0, tzinfo=IST)))
        self.assertFalse(poller_mod.is_open(
            datetime(2026, 11, 26, 15, 30, tzinfo=IST)))
        self.assertFalse(poller_mod.is_open(
            datetime(2026, 11, 28, 10, 0, tzinfo=IST)))

    def _indices_payload(self):
        return {"data": [
            {"index": "NIFTY 50", "last": 25791.95, "percentChange": 0.42,
             "variation": 108.1, "previousClose": 25683.85, "open": 25700.0,
             "pe": 22.5, "advances": 32, "declines": 18,
             "yearHigh": 26000.0, "yearLow": 22000.0,
             "oneWeekAgoVal": 25500.0, "perChange30d": 1.2,
             "perChange365d": 5.6},
            {"index": "NIFTY BANK", "last": 63404.95, "percentChange": -0.1,
             "variation": -63.0, "previousClose": 63468.0, "open": 63500.0,
             "pe": None, "advances": 5, "declines": 7,
             "yearHigh": 65000.0, "yearLow": 50000.0,
             "oneWeekAgoVal": 63000.0, "perChange30d": 0.5,
             "perChange365d": 8.0},
            {"index": "SENSEX", "last": 83929.48, "percentChange": 0.3,
             "variation": 250.0, "previousClose": 83679.0, "open": 83800.0,
             "pe": 23.0, "advances": 20, "declines": 10,
             "yearHigh": 85000.0, "yearLow": 70000.0,
             "oneWeekAgoVal": 83000.0, "perChange30d": 1.0,
             "perChange365d": 6.0},
            {"index": "NIFTY MIDCAP 100", "last": 50000.0,
             "percentChange": 0.1, "variation": 50.0,
             "previousClose": 49950.0},
        ]}

    def test_parse_all_indices(self):
        out = poller_mod.parse_all_indices(self._indices_payload())
        self.assertEqual(set(out), {"NIFTY", "BANKNIFTY", "SENSEX"})
        self.assertEqual(out["NIFTY"]["last"], 25791.95)
        self.assertEqual(out["NIFTY"]["prev"], 25683.85)
        self.assertAlmostEqual(out["NIFTY"]["w1"], 1.14, places=2)
        self.assertIsNone(out["BANKNIFTY"]["pe"])
        self.assertEqual(out["SENSEX"]["adv"], 20)

    def _spark_payload(self):
        return {"spark": {"result": [
            {"symbol": "^NSEI", "response": [{
                "timestamp": [1700000000, 1700000060],
                "indicators": {"quote": [{"close": [25790.0, None]}]},
                "meta": {"regularMarketPrice": 25790.0,
                         "chartPreviousClose": 25683.85,
                         "regularMarketTime": 1700000060}}]},
            {"symbol": "^NSEBANK", "response": [{
                "timestamp": [],
                "indicators": {"quote": [{"close": []}]},
                "meta": {}}]},
            {"symbol": "RELIANCE.NS", "response": []},
        ]}}

    def test_parse_spark(self):
        out = poller_mod.parse_spark(self._spark_payload())
        self.assertEqual(set(out), {"^NSEI"})
        self.assertEqual(out["^NSEI"]["ltp"], 25790.0)
        self.assertEqual(out["^NSEI"]["prev"], 25683.85)
        # The None close is dropped — only one 1-minute point survives.
        self.assertEqual(len(out["^NSEI"]["closes"]), 1)
        self.assertEqual(out["^NSEI"]["closes"][0]["c"], 25790.0)

    def test_spark_429_sets_global_backoff(self):
        import time as _time
        from unittest.mock import patch
        with patch.object(poller_mod, "fetch_spark",
                          side_effect=poller_mod.PollerError(
                              "HTTP 429", status=429)):
            poller_mod._tick_spark_fast()
        self.assertGreater(poller_mod.STATE["slow_until"], _time.time())
        self.assertGreaterEqual(poller_mod.STATE["errors"], 1)

    def test_tick_spark_skipped_during_backoff(self):
        import time as _time
        from unittest.mock import Mock, patch
        poller_mod.STATE["slow_until"] = _time.time() + 1000
        spy = Mock(side_effect=AssertionError("must not fetch"))
        with patch.object(poller_mod, "fetch_spark", spy):
            poller_mod._tick_spark_fast()
        spy.assert_not_called()

    def test_spark_fast_fetches_only_indices(self):
        from unittest.mock import patch
        with patch.object(poller_mod, "fetch_spark",
                          return_value={}) as spy:
            poller_mod._tick_spark_fast()
        spy.assert_called_once()
        self.assertEqual(spy.call_args.args[0],
                         ["^NSEI", "^NSEBANK", "^BSESN"])

    def test_spark_full_fetches_whole_universe(self):
        from unittest.mock import patch
        from bhanu import data as _data
        with patch.object(poller_mod, "fetch_spark",
                          return_value={}) as spy:
            poller_mod._tick_spark_full()
        spy.assert_called_once()
        self.assertEqual(set(spy.call_args.args[0]),
                         set(_data.UNIVERSE.values()))

    def test_live_cadence_is_seconds_not_minutes(self):
        # The live quote feed must tick every 2 seconds, never on a
        # 60-second cycle — that is the whole point of real-time.
        # (26 Nov 15:21 — Bhanu: 5 s wait is not trading.)
        self.assertEqual(poller_mod.POLL_SPARK_FAST_S, 2)
        self.assertEqual(poller_mod.POLL_SPARK_FULL_FAST_S, 30)

    def test_get_spark_reader(self):
        poller_mod.STATE["spark"]["^NSEI"] = {
            "ltp": 25790.0, "prev": 25683.85, "time": 1700000000,
            "closes": [{"t": 1700000000, "c": 25790.0}]}
        got = poller_mod.get_spark("^NSEI")
        self.assertEqual(got["ltp"], 25790.0)
        self.assertEqual(got["closes"][0]["c"], 25790.0)
        self.assertIsNone(poller_mod.get_spark("^UNKNOWN"))

    def test_get_quotes_uses_stale_spark_when_no_nse_quote(self):
        # SENSEX is a BSE index: NSE never lists it, and Yahoo's BSE
        # feed runs ~15 min stale — the card must still show the last
        # good value instead of "no live quote yet".
        import time as _time
        poller_mod.STATE["indices"].clear()
        poller_mod.STATE["spark"]["^BSESN"] = {
            "ltp": 83960.42, "prev": 83000.0,
            "time": _time.time() - 900, "closes": []}
        out = poller_mod.get_quotes()
        sensex = [q for q in out if q["symbol"] == "^BSESN"][0]
        self.assertEqual(sensex["price"], 83960.42)
        self.assertIsNone(sensex["error"])

    def test_get_quotes_empty_when_no_state(self):
        self.assertIsNone(poller_mod.get_quotes())

    def test_get_quotes_prefers_fresh_spark(self):
        import time as _time
        poller_mod.STATE["indices"] = poller_mod.parse_all_indices(
            self._indices_payload())
        poller_mod.STATE["spark"] = {
            "^NSEI": {"ltp": 25100.0, "prev": 24900.0,
                      "time": int(_time.time()), "closes": []}}
        quotes = poller_mod.get_quotes()
        nifty = next(q for q in quotes if q["symbol"] == "^NSEI")
        self.assertEqual(nifty["price"], 25100.0)
        # Price takes the fresh spark ltp, but the previous close is
        # pinned to NSE (25683.85) — never Yahoo's 24900.0 — so the %
        # is always vs the same base.
        self.assertEqual(nifty["prev_close"], 25683.85)
        self.assertAlmostEqual(nifty["change_pct"], -2.27, places=2)
        self.assertIsNone(nifty["error"])

    def test_get_quotes_prev_close_never_flips(self):
        # Regression (26 Nov 2026, Bhanu's PC): NIFTY prev close
        # flipped between NSE 25,819.25 and Yahoo 25,349.70 every
        # time the spark went fresh/stale, and % change recomputed
        # against whichever base was live. The pin must hold one
        # value across fresh/stale alternation.
        # (Prev pins are per-day and persist in STATE: clear them so
        # this test starts hermetic — an earlier test class may have
        # left a Yahoo fallback pin behind.)
        poller_mod.STATE["prev_pinned"] = {}
        import time as _time
        payload = {"data": [
            {"index": "NIFTY 50", "last": 25900.0, "percentChange": 0.31,
             "variation": 80.75, "previousClose": 25819.25, "open": 25850.0},
            {"index": "NIFTY BANK", "last": 63600.0, "percentChange": -0.16,
             "variation": -103.25, "previousClose": 63703.25,
             "open": 63650.0},
        ]}
        poller_mod.STATE["indices"] = poller_mod.parse_all_indices(payload)
        for i in range(6):
            fresh = (i % 2 == 0)
            poller_mod.STATE["spark"] = {
                "^NSEI": {"ltp": 25900.0 + i, "prev": 25349.70,
                          "time": int(_time.time()) if fresh
                          else int(_time.time()) - 600, "closes": []},
                "^NSEBANK": {"ltp": 63600.0 - i, "prev": 62585.40,
                             "time": int(_time.time()) if fresh
                             else int(_time.time()) - 600, "closes": []},
            }
            quotes = poller_mod.get_quotes()
            nifty = next(q for q in quotes if q["symbol"] == "^NSEI")
            bank = next(q for q in quotes if q["symbol"] == "^NSEBANK")
            self.assertEqual(nifty["prev_close"], 25819.25)
            self.assertEqual(bank["prev_close"], 63703.25)
            # % always vs the pinned base, never vs Yahoo's prev.
            self.assertAlmostEqual(
                nifty["change_pct"],
                round((nifty["price"] / 25819.25 - 1) * 100, 2), places=2)
            self.assertAlmostEqual(
                bank["change_pct"],
                round((bank["price"] / 63703.25 - 1) * 100, 2), places=2)

    def test_get_quotes_prev_close_upgrades_yahoo_to_nse_once(self):
        # Engine starts on Yahoo only (NSE not in yet): prev is the
        # Yahoo fallback. The moment NSE data arrives the pin upgrades
        # to NSE — once — and never flips back to Yahoo afterwards.
        import time as _time
        poller_mod.STATE["prev_pinned"] = {}
        poller_mod.STATE["indices"] = {}
        poller_mod.STATE["spark"] = {
            "^NSEI": {"ltp": 25900.0, "prev": 25349.70,
                      "time": int(_time.time()), "closes": []}}
        first = next(q for q in poller_mod.get_quotes()
                     if q["symbol"] == "^NSEI")
        self.assertEqual(first["prev_close"], 25349.70)
        poller_mod.STATE["indices"] = poller_mod.parse_all_indices(
            {"data": [{"index": "NIFTY 50", "last": 25900.0,
                       "percentChange": 0.31, "variation": 80.75,
                       "previousClose": 25819.25, "open": 25850.0}]})
        second = next(q for q in poller_mod.get_quotes()
                      if q["symbol"] == "^NSEI")
        self.assertEqual(second["prev_close"], 25819.25)
        third = next(q for q in poller_mod.get_quotes()
                     if q["symbol"] == "^NSEI")
        self.assertEqual(third["prev_close"], 25819.25)

    def test_get_quotes_prev_close_stays_when_nse_drops(self):
        # NSE fetch failing later must not hand the pin back to Yahoo:
        # the last good NSE prev stays until the trading day changes.
        import time as _time
        poller_mod.STATE["prev_pinned"] = {}
        poller_mod.STATE["indices"] = poller_mod.parse_all_indices(
            self._indices_payload())
        poller_mod.STATE["spark"] = {
            "^NSEI": {"ltp": 25100.0, "prev": 24900.0,
                      "time": int(_time.time()), "closes": []}}
        first = next(q for q in poller_mod.get_quotes()
                     if q["symbol"] == "^NSEI")
        self.assertEqual(first["prev_close"], 25683.85)
        poller_mod.STATE["indices"] = {}
        second = next(q for q in poller_mod.get_quotes()
                      if q["symbol"] == "^NSEI")
        self.assertEqual(second["prev_close"], 25683.85)

    def test_get_quotes_stale_spark_ignored(self):
        import time as _time
        poller_mod.STATE["indices"] = poller_mod.parse_all_indices(
            self._indices_payload())
        poller_mod.STATE["spark"] = {
            "^NSEI": {"ltp": 25100.0, "prev": 24900.0,
                      "time": int(_time.time()) - 600, "closes": []}}
        quotes = poller_mod.get_quotes()
        nifty = next(q for q in quotes if q["symbol"] == "^NSEI")
        self.assertEqual(nifty["price"], 25791.95)

    def test_snapshot_roundtrip(self):
        import tempfile
        from unittest.mock import patch
        poller_mod.STATE["indices"] = poller_mod.parse_all_indices(
            self._indices_payload())
        poller_mod.STATE["intraday"] = {
            "^NSEI": {"symbol": "^NSEI", "line": [{"time": 1, "value": 2.0}],
                      "candles": [], "error": None, "stale": False,
                      "as_of": ""}}
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(poller_mod, "SNAPSHOT_PATH",
                              Path(tmp) / "poller_snapshot.json"), \
                 patch.object(poller_mod, "is_open", return_value=True):
                poller_mod._tick_snapshot()
                poller_mod.STATE["indices"] = {}
                poller_mod.STATE["intraday"] = {}
                self.assertTrue(poller_mod._load_snapshot())
        self.assertEqual(poller_mod.STATE["indices"]["NIFTY"]["last"],
                         25791.95)
        self.assertEqual(poller_mod.STATE["intraday"]["^NSEI"]["line"],
                         [{"time": 1, "value": 2.0}])

    def test_start_stop_idempotent(self):
        from unittest.mock import patch
        noop = (("noop", 3600, 3600, lambda: None),)
        with patch.object(poller_mod, "_JOBS", noop):
            self.assertTrue(poller_mod.start())
            first = len(poller_mod._threads)
            self.assertEqual(first, 1)
            self.assertTrue(poller_mod.start())
            self.assertEqual(len(poller_mod._threads), 1)
            self.assertTrue(poller_mod.is_running())
            poller_mod.stop()
            self.assertFalse(poller_mod.is_running())

    def test_status_shape(self):
        st = poller_mod.status()
        for key in ("running", "started_at", "market_open", "ticks",
                    "symbols_scanned", "spark_symbols", "orb_hits",
                    "news_checked_at", "timemachine_replays", "snapshot_at",
                    "errors", "backoff_until", "fresh"):
            self.assertIn(key, st)

    def test_breakdown_shows_robot_freshness_when_running(self):
        from web import app as app_mod
        sig = {"decision": "ENTER", "symbol": "NIFTY", "direction": "long",
               "entry": 25730.0, "stop": 25650.0, "tp1": 25810.0,
               "tp2": 25890.0, "tp3": 25970.0, "summary": "ok",
               "factors": {}}
        html = app_mod._signal_breakdown_html(sig)
        self.assertNotIn("Robot", html)
        poller_mod.STATE["running"] = True
        poller_mod.STATE["ticks"] = {
            "intraday": "2026-11-26 14:30:00 IST"}
        poller_mod.STATE["news_checked_at"] = "2026-11-26 14:29:00 IST"
        poller_mod.STATE["symbols_scanned"] = 39
        html = app_mod._signal_breakdown_html(sig)
        self.assertIn("Robot", html)
        self.assertIn("14:30:00", html)


class TestRebrand(unittest.TestCase):
    """The product is only ever 'Bhanu Indian Day Trader': no other
    dashboard name anywhere in the repo (code, docs, UI), and the home
    page carries the robot + mini sections with no page-load fetch."""

    def test_no_other_dashboard_name_in_repo(self):
        root = Path(__file__).resolve().parent.parent
        skip_dirs = {".venv", "__pycache__", "data", ".git"}
        forbidden = ("Market Lab", "market-lab", "market_lab", "MarketLab")
        hits = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in skip_dirs for part in path.parts):
                continue
            if path.name == "test_bhanu.py":
                continue  # the lock test itself names the forbidden string
            if path.suffix.lower() not in (
                    ".py", ".md", ".txt", ".html", ".js", ".bat", ".sh",
                    ".yml", ".yaml", ".json"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for needle in forbidden:
                if needle in text:
                    hits.append(f"{path.relative_to(root)}: {needle!r}")
        self.assertEqual(hits, [])

    def test_product_name_everywhere(self):
        root = Path(__file__).resolve().parent.parent
        self.assertIn("Bhanu Indian Day Trader",
                      (root / "README.md").read_text(encoding="utf-8"))
        self.assertIn("Bhanu Indian Day Trader",
                      (root / "web" / "app.py").read_text(encoding="utf-8"))

    def test_home_has_robot_and_mini_sections(self):
        from web import app as app_mod
        html = app_mod.home("en")
        for needle in ("id='robot-status'", "id='levels-mini'",
                       "id='tm-mini'", "bhanuRobotFetch",
                       "bhanuLevelsMiniFetch", "bhanuTmMiniFetch"):
            self.assertIn(needle, html)
        # No page-load fetch: the page HTML must not embed upstream URLs.
        self.assertNotIn("finance.yahoo.com", html)
        self.assertNotIn("nseindia.com", html)

    def test_api_poller_shape(self):
        from web import app as app_mod
        out = app_mod._api_poller()
        self.assertIn("running", out)
        self.assertIn("ticks", out)
        self.assertIn("fresh", out)

    def test_api_robot_warming_up_when_engine_off(self):
        from web import app as app_mod
        out = app_mod._api_robot()
        self.assertIn("BOT", out["html"])
        self.assertIn("warming up", out["html"])

    def test_levels_mini_stays_in_its_box(self):
        from unittest.mock import patch
        from web import app as app_mod
        result = {"run_at_ist": "2026-11-26 13:00:00 IST", "levels": [
            {"symbol": "NIFTY", "price": 25748.0,
             "nearest_support": 25650.0, "nearest_resistance": 25800.0,
             "signal": "short"}],
            "failed": []}
        app_mod._cache.pop("levels", None)
        with patch.object(levels_mod, "scan_levels", return_value=result):
            out = app_mod._api_levelsmini()
        self.assertIn("table-wrap", out["html"])
        self.assertIn("25,748.00", out["html"])


class TestLiveRealtime(unittest.TestCase):
    """Real-time, not refresh: quotes and index charts tick every 2 s
    and update in place. The 60-second cycle is only for slow sections
    (flow, sectors, news, ORB) whose data changes that slowly."""

    def setUp(self):
        poller_mod.STATE["spark"].clear()
        poller_mod.STATE["slow_until"] = 0.0

    def _seed_spark(self):
        poller_mod.STATE["spark"]["^NSEI"] = {
            "ltp": 25790.0, "prev": 25683.85, "time": 1700000060,
            "closes": [{"t": 1700000000, "c": 25785.0},
                       {"t": 1700000060, "c": 25790.0}]}

    def test_api_live_returns_tick_when_warmed(self):
        from web import app as app_mod
        self._seed_spark()
        out = app_mod._api_live({"symbol": ["^NSEI"]})
        self.assertEqual(out["ltp"], 25790.0)
        self.assertAlmostEqual(out["change_pct"], 0.41, places=1)
        self.assertEqual(len(out["line"]), 2)
        self.assertEqual(out["line"][-1]["value"], 25790.0)
        self.assertIsNone(out["error"])

    def test_api_live_honest_when_warming_up(self):
        from web import app as app_mod
        out = app_mod._api_live({"symbol": ["^NSEI"]})
        self.assertIsNone(out["ltp"])
        self.assertIn("warming up", out["error"])

    def test_api_live_rejects_unknown_symbol(self):
        from web import app as app_mod
        out = app_mod._api_live({"symbol": ["^FOO"]})
        self.assertIsNone(out["ltp"])
        self.assertIn("unknown symbol", out["error"])

    def test_api_live_uses_pinned_prev_close(self):
        # /api/live must report the same pinned previous close as
        # /api/quotes (NSE 25,819.25), never Yahoo's own 25,349.70 —
        # even though the spark entry itself carries Yahoo's value.
        import copy
        import time as _time
        from bhanu import poller as poller_mod
        saved_indices = copy.deepcopy(poller_mod.STATE["indices"])
        saved_spark = copy.deepcopy(poller_mod.STATE["spark"])
        saved_pinned = copy.deepcopy(poller_mod.STATE["prev_pinned"])
        try:
            poller_mod.STATE["indices"] = poller_mod.parse_all_indices(
                {"data": [{"index": "NIFTY 50", "last": 25900.0,
                           "percentChange": 0.31, "variation": 80.75,
                           "previousClose": 25819.25, "open": 25850.0}]})
            poller_mod.STATE["spark"]["^NSEI"] = {
                "ltp": 25900.0, "prev": 25349.70,
                "time": int(_time.time()),
                "closes": [{"t": int(_time.time()), "c": 25900.0}]}
            from web import app as app_mod
            out = app_mod._api_live({"symbol": ["^NSEI"]})
            self.assertEqual(out["prev"], 25819.25)
            self.assertAlmostEqual(
                out["change_pct"],
                round((25900.0 / 25819.25 - 1) * 100, 2), places=2)
        finally:
            poller_mod.STATE["indices"] = saved_indices
            poller_mod.STATE["spark"] = saved_spark
            poller_mod.STATE["prev_pinned"] = saved_pinned

    def test_line_charts_tick_live_not_on_60s_cycle(self):
        from web import app as app_mod
        js = app_mod._chart_js()
        # 2-second live tick machinery exists…
        self.assertIn("bhanuLiveLineTick", js)
        self.assertIn("bhanuStartLiveLine", js)
        self.assertIn("setInterval(run,2000)", js)
        self.assertIn("/api/live?symbol=", js)
        # …and the old 60-second full re-fetch of the line is gone.
        self.assertNotIn("fetchLine", js)
        # Quotes tick every 2 s too.
        self.assertIn("setInterval(function(){\n      bhanuQuoteFetch()",
                      js)

    def test_live_indicator_promises_seconds(self):
        from web import app as app_mod
        html = app_mod.home("en")
        self.assertIn("tick every 2 seconds", html)
        self.assertNotIn("Charts refresh every 60 seconds", html)

    def test_live_clock_ticks_every_second(self):
        from web import app as app_mod
        js = app_mod._live_js()
        self.assertIn("bhanuLiveClock", js)
        self.assertIn("setInterval(bhanuLiveClock, 1000)", js)
        # The clock shows the current time plus when data last
        # arrived — it never pretends a tick happened.
        self.assertIn("data-last", js)
        self.assertIn("data ", js)


class TestMarketLabLook(unittest.TestCase):
    """Same-to-same restyle markers: the reference look (palette,
    fonts, disclaimer, pill nav, LIVE dot, icard tiles, spaghetti +
    waterfall, horizon sector map, trade panels) — without ever
    naming the reference product in the repo."""

    def test_disclaimer_strip_and_fonts(self):
        from web import app as app_mod
        page = app_mod._page("en", "x", "")
        self.assertIn('id="ml-disc"', page)
        self.assertIn("Paper trading only", page)
        self.assertIn("not investment advice", page)
        self.assertIn("Barlow+Condensed", page)
        self.assertIn("family=Inter", page)
        self.assertIn("JetBrains+Mono", page)

    def test_palette_vars(self):
        import re
        from web import app as app_mod
        css = re.search(r"<style>(.*?)</style>",
                        app_mod._page("en", "x", ""), re.S).group(1)
        for var in ("--bg:#edf0f3", "--accent:#2563eb", "--up:#2f9e5f",
                    "--down:#d6455d", "--upbg:#e7f5ec", "--downbg:#fbe9ec",
                    "--upmid:#9fd8b5", "--downmid:#f0a9b5",
                    "--gold:#b7791f"):
            self.assertIn(var, css)
        for cls in (".tpanel", ".icard", ".tile", ".sm-stage",
                    ".sm-waterfall", ".hz-wrap", ".row", ".niftysep",
                    ".badge", ".pill", ".dot", ".ml-h", ".seg",
                    ".ml-top", "nav.seg"):
            self.assertIn(cls, css)

    def test_home_sections_in_reference_order(self):
        from web import app as app_mod
        html = app_mod.home("en")
        order = ["SMART MONEY", "SECTOR MAP", "robot-status", "NEWS",
                 "ORB Hits", "Levels — indices", "Time Machine — today"]
        idx = [html.index(marker) for marker in order]
        self.assertEqual(idx, sorted(idx))
        for marker in ("flow-chart-counts", "flow-chart-waterfall",
                       "sector-map-list", "hz-wrap", "sm-stage",
                       "Sector Map ↓", "Smart money ↑", "LIVE · NSE indices"):
            self.assertIn(marker, html)

    def test_index_tiles_have_sparklines(self):
        from web import app as app_mod
        html = app_mod.home("en")
        for slug in ("nsei", "nsebank", "bsesn"):
            self.assertIn(f"q-{slug}-card", html)
            self.assertIn(f"q-{slug}-spark", html)
            self.assertIn(f"q-{slug}-price", html)
            self.assertIn(f"q-{slug}-prev", html)
        js = app_mod._chart_js()
        self.assertIn("bhanuSpark(", js)
        self.assertIn("bhanuSparkFor(", js)

    def test_live_dot_not_bullet(self):
        from web import app as app_mod
        html = app_mod.home("en")
        self.assertIn("<span class='dot'></span> LIVE · last update", html)
        self.assertNotIn("● LIVE", html)
        js = app_mod._chart_js()
        self.assertIn("mlpulse", app_mod._page("en", "x", ""))
        self.assertIn('class="dot"', app_mod._live_js())

    def test_flow_is_spaghetti_svg_not_line_chart(self):
        from web import app as app_mod
        js = app_mod._chart_js()
        # Dashed blue NIFTY, thin green/red lines, sector labels.
        self.assertIn('stroke-dasharray="6 4"', js)
        self.assertIn("bhanuWaterfall(", js)
        self.assertIn("STRONGEST", js)
        self.assertIn("WEAKEST", js)
        # The old Lightweight flow chart (39 line series) is gone —
        # the spaghetti is hand-drawn SVG now.
        self.assertNotIn("window.bhanuFlow", js)

    def test_sector_map_is_horizon_with_running_badges(self):
        from web import app as app_mod
        js = app_mod._chart_js()
        self.assertIn("RUNNING", js)
        self.assertIn("Themes beating NIFTY", js)
        self.assertIn("niftysep", js)
        self.assertIn("TECH & MEDIA", js)
        self.assertIn("FINANCIALS", js)

    def test_robot_is_trade_panel(self):
        from unittest.mock import patch
        from web import app as app_mod
        from bhanu import poller as poller_mod
        with patch.object(
                poller_mod, "get_levels",
                return_value={"levels": [
                    {"symbol": "NIFTY", "signal": "long",
                     "entry": 25730.0, "stop": 25702.5}]}):
            html = app_mod._robot_html()
        for needle in ("tpanel", "AAJ KA BEST SETUP", "ENTRY", "STOP",
                       "R:R", "TP1 — 1R", "TP2 — 2R", "TP3 — 3R",
                       "25,730.00", "25,702.50", "25,757.50"):
            self.assertIn(needle, html)
        self.assertIn("class='rr'", html)

    def test_signal_breakdown_is_trade_panel(self):
        from web import app as app_mod
        sig = {"decision": "ENTER", "symbol": "NIFTY", "direction": "long",
               "summary": "ORB trigger, all factors agree",
               "entry": 25730.0, "stop": 25702.5,
               "tp1": 25757.5, "tp2": 25785.0, "tp3": 25812.5,
               "size_factor": 1.0,
               "factors": {
                   "orb": {"verdict": "TRIGGER", "reason": "close above range"},
                   "levels": {"verdict": "SUPPORT", "reason": "above support"},
                   "news": {"verdict": "SUPPORT", "reason": "no bad news"},
                   "mc": {"verdict": "ALLOW", "reason": "target hit 40%"},
                   "volume": {"verdict": "STRONG", "reason": "1.8x average"}}}
        html = app_mod._signal_breakdown_html(sig)
        for needle in ("tpanel", "SIGNAL — NIFTY <span class='opt-call'>CALL</span>",
                       "ENTRY", "STOP",
                       "R:R", "TP1 — 1R", "TP2 — 2R", "TP3 — 3R",
                       "News — SUPPORT", "Time Machine — ALLOW",
                       "Volume — STRONG", "class='rr'"):
            self.assertIn(needle, html)

    def test_direction_shows_as_call_put(self):
        """Trade direction is shown as CALL (green) / PUT (red), never
        long/short — display only, internal values stay long/short."""
        from web import app as app_mod
        from bhanu import signal as signal_mod
        self.assertEqual(
            app_mod._opt("long"), "<span class='opt-call'>CALL</span>")
        self.assertEqual(
            app_mod._opt("short"), "<span class='opt-put'>PUT</span>")
        self.assertEqual(app_mod._opt_text("long"), "CALL")
        self.assertEqual(app_mod._opt_text("short"), "PUT")
        self.assertEqual(signal_mod.opt_label("long"), "CALL")
        self.assertEqual(signal_mod.opt_label("short"), "PUT")
        sig = {"decision": "HALF SIZE", "symbol": "NIFTY",
               "direction": "short", "summary": "x",
               "entry": 25752.5, "stop": 25822.33,
               "tp1": 25682.67, "tp2": 25612.84, "tp3": 25543.01,
               "size_factor": 0.5, "factors": {}}
        html = app_mod._signal_breakdown_html(sig)
        self.assertIn("SIGNAL — NIFTY <span class='opt-put'>PUT</span>", html)
        self.assertNotIn("short", html)

    def test_stock_page_shell_renders(self):
        from web import app as app_mod
        html = app_mod.stock_page("en", {"symbol": ["RELIANCE"]})
        for needle in ("stock-chart", "tfbtn", "opill", "stock-setup",
                       "stock-summary", "Summary load ho raha hai…",
                       "bhanuStockLoad('RELIANCE','5m')", "Order block",
                       "Fair value gaps", "Liquidity", "VWAP", "Trades"):
            self.assertIn(needle, html)
        for iv in ("1m", "5m", "15m", "1H", "D"):
            self.assertIn("data-iv='%s'" % iv, html)
        self.assertIn("bhanuToggleOverlay", html)

    def test_stock_resolve_sector_and_rank_never_invented(self):
        from bhanu import stock as stock_mod
        label, yahoo, in_universe = stock_mod.resolve_symbol("reliance")
        self.assertEqual((label, yahoo, in_universe),
                         ("RELIANCE", "RELIANCE.NS", True))
        self.assertEqual(stock_mod.sector_for("RELIANCE"), "Energy")
        self.assertEqual(stock_mod.sector_for("NIFTY"), "Index")
        self.assertIsNone(stock_mod.sector_for("XYZABC"))
        with self.assertRaises(ValueError):
            stock_mod.resolve_symbol("XYZABC")
        # Rank is over our own universe only — never "OF 118".
        series = [{"symbol": "S%02d" % i, "change_pct": i * 0.1,
                   "up": i >= 20} for i in range(38)]
        series.append({"symbol": "NIFTY", "change_pct": 0.25, "up": True})
        rank = stock_mod.rank_in_flow("NIFTY", {"series": series})
        self.assertEqual(rank, {"rank": 36, "total": 39, "up": True})

    def test_stock_sessions_keep_short_stock_days(self):
        # Yahoo drops a trailing 5m bar or two for stocks (RELIANCE
        # sessions run 72-74 bars, NIFTY 75). A session that starts at
        # 09:15 and reaches the flat-by bar still counts; only a
        # session that ends before flat-by is dropped.
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        from bhanu import stock as stock_mod
        ist = ZoneInfo("Asia/Kolkata")
        base = datetime(2026, 11, 23, 9, 15, tzinfo=ist)
        bars = []
        for day, n in ((0, 75), (1, 73), (2, 60)):
            for i in range(n):
                bars.append({"time": base + timedelta(days=day, minutes=5 * i),
                             "open": 1.0, "high": 1.0, "low": 1.0,
                             "close": 1.0})
        sessions = stock_mod._sessions_from_bars(bars)
        self.assertEqual([len(s) for s in sessions], [75, 73])

    def test_stock_chart_dedupes_close_labels(self):
        # Bhanu's screenshot: PDL 25,619.00 and TP 25,612.84 printed on
        # top of each other. The lower-priority label must hide; both
        # lines are still drawn.
        from bhanu import charts as charts_mod
        lines = [
            {"price": 25619.0, "label": "PDL", "group": "levels"},
            {"price": 25612.84, "label": "TP", "group": "entry"},
            {"price": 25800.0, "label": "PDH", "group": "levels"},
        ]
        out = charts_mod._dedupe_line_labels(lines, 1000.0)
        self.assertEqual(len(out), 3)
        by_label = {ln["label"]: ln for ln in out}
        self.assertTrue(by_label["TP"].get("label_visible", True))
        self.assertFalse(by_label["PDL"].get("label_visible", True))
        self.assertTrue(by_label["PDH"].get("label_visible", True))

    def test_stock_chart_reports_overlay_availability(self):
        from datetime import datetime
        from unittest.mock import patch
        from bhanu import charts as charts_mod
        from bhanu import data as data_mod
        today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d 09:15")
        bars = data_mod.make_synthetic_bars(
            60, start_price=25700.0, start=today, freq="5min")
        row = {"symbol": "NIFTY", "signal": "long", "entry": 25730.0,
               "stop": 25702.5, "target": 25785.0,
               "nearest_support": 25650.0, "nearest_resistance": 25800.0,
               "pdh": 25800.0, "pdl": 25550.0,
               "orb_high": 25718.0, "orb_low": 25687.0}
        with patch.object(charts_mod, "fetch_intraday_bars",
                          return_value=bars):
            payload = charts_mod.stock_chart("^NSEI", "NIFTY", "5m", row=row)
        self.assertTrue(payload["candles"])
        self.assertEqual(set(payload["overlays"]),
                         {"orderblock", "fvg", "structure", "liquidity",
                          "vwap", "trades"})
        self.assertFalse(payload["overlays"]["liquidity"])
        self.assertTrue(payload["overlays"]["vwap"])
        # No two visible axis labels sit on top of each other: any two
        # visible lines are more than the 4% dedupe threshold apart.
        price_range = float(bars["high"].max() - bars["low"].min())
        visible = sorted(
            ln["price"] for ln in payload["lines"]
            if ln.get("label_visible", True))
        for a, b in zip(visible, visible[1:]):
            self.assertGreater(b - a, 0.04 * price_range)

    def test_stock_chart_daily_shows_past_trades(self):
        from unittest.mock import patch
        from bhanu import charts as charts_mod
        from bhanu import data as data_mod
        daily = data_mod.make_synthetic_bars(
            180, start_price=25700.0, start="2026-08-03 09:15", freq="B")
        first_date = str(daily.index[10].date())
        trades = [{"date": first_date, "direction": "long",
                   "entry": 25730.0, "stop": 25702.5, "target": 25785.0,
                   "outcome_r": 2.0, "result": "win"}]
        row = {"symbol": "NIFTY", "signal": "long", "entry": 25730.0,
               "stop": 25702.5, "target": 25785.0,
               "nearest_support": 25650.0, "nearest_resistance": 25800.0,
               "pdh": 25800.0, "pdl": 25550.0,
               "orb_high": 25718.0, "orb_low": 25687.0}
        with patch.object(data_mod, "fetch_daily_bars", return_value=daily):
            payload = charts_mod.stock_chart(
                "^NSEI", "NIFTY", "D", row=row, past_trades=trades)
        self.assertTrue(payload["candles"])
        self.assertFalse(payload["overlays"]["vwap"])  # no VWAP on daily
        self.assertTrue(payload["overlays"]["trades"])
        self.assertTrue(any(m.get("group") == "trades"
                            for m in payload["markers"]))

    def test_api_stock_shows_real_setup(self):
        from unittest.mock import patch
        from web import app as app_mod
        from bhanu import charts as charts_mod
        from bhanu import data as data_mod
        from bhanu import levels as levels_mod
        from bhanu import poller as poller_mod
        from bhanu import stock as stock_mod
        app_mod._cache.clear()
        series = [{"symbol": "S%02d" % i, "change_pct": i * 0.1,
                   "up": i >= 20} for i in range(38)]
        series.append({"symbol": "NIFTY", "change_pct": 0.25, "up": True})
        row = {"symbol": "NIFTY", "signal": "short", "entry": 25752.5,
               "stop": 25822.33, "target": 25612.84, "price": 25884.5,
               "orb_high": 25718.0, "orb_low": 25687.0,
               "nearest_support": 25650.0, "nearest_resistance": 25800.0,
               "pdh": 25800.0, "pdl": 25550.0}
        sig = {"decision": "ENTER", "symbol": "NIFTY", "direction": "short",
               "summary": "ORB trigger, all factors agree",
               "entry": 25752.5, "stop": 25822.33,
               "tp1": 25682.67, "tp2": 25612.84, "tp3": 25543.01}
        hist = {"available": True, "sessions": 42,
                "first_day": "2026-09-28", "last_day": "2026-11-26",
                "long": {"fires": 17, "wins": 7, "losses": 7, "neither": 3,
                         "avg_r": 0.49, "win_rate_pct": 50.0, "net_r": 8.33},
                "short": {"fires": 9, "wins": 0, "losses": 5, "neither": 4,
                          "avg_r": -0.362, "win_rate_pct": 0.0, "net_r": -3.26},
                "trades": []}
        chart = {"symbol": "NIFTY",
                 "candles": [{"time": 1, "open": 1, "high": 1, "low": 1,
                              "close": 1}],
                 "vwap": [], "lines": [], "markers": [], "boxes": [],
                 "overlays": {"orderblock": True, "fvg": True,
                              "structure": True, "liquidity": False,
                              "vwap": True, "trades": False},
                 "error": None}
        with patch.object(data_mod, "fetch_quote",
                          return_value={"price": 25884.5,
                                        "prev_close": 25819.25,
                                        "change_pct": 0.25}), \
             patch.object(poller_mod, "get_flow",
                          return_value={"series": series}), \
             patch.object(levels_mod, "compute_levels", return_value=row), \
             patch.object(app_mod, "_entry_signal_for", return_value=sig), \
             patch.object(stock_mod, "orb_history", return_value=hist), \
             patch.object(charts_mod, "stock_chart", return_value=chart):
            result = app_mod._api_stock({"symbol": ["NIFTY"],
                                         "interval": ["5m"]})
        self.assertIsNone(result["error"])
        self.assertEqual(result["rank"], {"rank": 36, "total": 39, "up": True})
        self.assertEqual(result["sector"], "Index")
        self.assertFalse(result["chart"]["overlays"]["liquidity"])
        header = result["header_html"]
        self.assertIn("BULLISH #36 OF 39", header)
        self.assertNotIn("OF 118", header)
        self.assertIn("25,884.50", header)
        setup = result["setup_html"]
        for needle in ("SETUP READY", "25,752.50", "69.83 pts",
                       "TP1 — 1R", "TP2 — 2R", "TP3 — 3R",
                       "opt-put", "Trades", "36.8%", "+5.07R"):
            self.assertIn(needle, setup)
        self.assertIn("NSE ke 50 sabse bade stocks", result["summary_html"])

    def test_api_stock_unknown_symbol(self):
        from web import app as app_mod
        result = app_mod._api_stock({"symbol": ["XYZABC"]})
        self.assertIn("error", result)
        self.assertTrue(result["error"])

    def test_stock_setup_watching_when_no_signal(self):
        from web import app as app_mod
        row = {"orb_high": 25718.0, "orb_low": 25687.0,
               "nearest_support": 25650.0, "nearest_resistance": 25800.0}
        stats = {"available": False,
                 "reason": "not enough data — only 3 full sessions (need 20)"}
        html = app_mod._stock_setup_html(None, row, stats)
        for needle in ("WATCHING", "Buy above 25,718.00",
                       "sell below 25,687.00", "not enough data"):
            self.assertIn(needle, html)

    def test_home_renders_fast(self):
        import time
        from web import app as app_mod
        start = time.monotonic()
        app_mod.home("en")
        self.assertLess(time.monotonic() - start, 0.5)


class TestSmoothCharts(unittest.TestCase):
    """Smooth, continuous charts (26 Nov 15:23): the line glides from
    the old price to the new one on every 2 s tick — it never stops
    and then jumps. The last point updates in place (series.update /
    SVG attribute), the whole series is re-set only on the 5-minute
    rebase, and the header price glides along with the animation."""

    def test_tick_glides_last_point_in_place(self):
        from web import app as app_mod
        js = app_mod._chart_js()
        # Tween machinery exists.
        self.assertIn("bhanuAnimLast", js)
        self.assertIn("bhanuEase", js)
        self.assertIn("requestAnimationFrame", js)
        self.assertIn("bhanuSvgLineLast", js)
        # The tick uses the tween…
        tick = js[js.index("function bhanuLiveLineTick"):
                  js.index("function bhanuStartLiveLine")]
        self.assertIn("bhanuAnimLast(", tick)
        # …and never re-sets the whole series or rebuilds the SVG.
        self.assertNotIn("setData(", tick)
        self.assertNotIn("bhanuSvgLine(st.el", tick)

    def test_svg_keeps_in_place_handles(self):
        from web import app as app_mod
        js = app_mod._chart_js()
        self.assertIn("el._bhanuPath", js)
        self.assertIn("setAttribute('d',d)", js)
        self.assertIn("setAttribute('cy'", js)

    def test_svg_last_point_clamps_inside_canvas(self):
        # A tick outside the initial min/max pins the dot to the
        # chart edge instead of flying off the canvas (cy < 0 or
        # cy > height) until the 5-minute rebase re-scales.
        from web import app as app_mod
        js = app_mod._chart_js()
        self.assertIn("Yc(val)", js)
        self.assertIn("Math.max(pad,Math.min(H-pad,y))", js)
        self.assertIn("el._bhanuPad", js)

    def test_tick_cadence_is_two_seconds(self):
        from web import app as app_mod
        js = app_mod._chart_js()
        self.assertIn("setInterval(run,2000)", js)
        # Rebase still every ~5 minutes (150 ticks at 2 s), not 60.
        self.assertIn("ticks%150===0", js)
        self.assertNotIn("ticks%60===0", js)

    def test_pages_render_instantly(self):
        # Every page must open in 2–3 s like the reference: the page
        # shell renders from local data only; slow sections load
        # lazily afterwards. Time the renders (cold cache).
        import time
        from unittest.mock import patch
        from web import app as app_mod
        from bhanu import options as options_mod
        with patch.object(options_mod, "fetch_chain",
                          return_value={"strikes": []}):
            for fn in (lambda: app_mod.home("en"),
                       lambda: app_mod.orb_page("en", {"symbol": ["NIFTY"]}),
                       lambda: app_mod.levels_page("en", {"symbol": ["NIFTY"]}),
                       lambda: app_mod.sectors_page("en"),
                       lambda: app_mod.chain_page("en", {"symbol": ["NIFTY"]}),
                       lambda: app_mod.sizing_page("en", {}),
                       lambda: app_mod.paper_page("en"),
                       lambda: app_mod.skills_page("en"),
                       lambda: app_mod.timemachine_page("en")):
                start = time.monotonic()
                fn()
                self.assertLess(time.monotonic() - start, 2.0,
                                "page render took over 2 s")


if __name__ == "__main__":
    unittest.main()
