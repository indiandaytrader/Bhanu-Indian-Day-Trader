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
from bhanu import entry as entry_mod
from bhanu import exit as exit_mod
from bhanu import i18n as i18n_mod
from bhanu import levels as levels_mod
from bhanu import news as news_mod
from bhanu import paper as paper_mod
from bhanu import risk as risk_mod
from bhanu import scanner as scanner_mod
from bhanu import sizing as sizing_mod
from bhanu import skills as skills_mod
from bhanu import timecheck as timecheck_mod
from bhanu import timemachine as tm_mod

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
            html = app_mod.home("hi")
        # Headlines are visible without the user doing anything.
        self.assertIn("Nifty rallies on FII buying", html)
        self.assertIn("RBI keeps repo rate unchanged", html)
        # Bot language, and the blocked NSE source is stated honestly.
        self.assertIn("Bot ne abhi check kiya", html)
        self.assertIn("NSE announcements unavailable", html)

    def test_home_news_failure_is_honest(self):
        from unittest.mock import patch
        from web import app as app_mod
        app_mod._cache.clear()
        with patch.object(news_mod, "check_symbol", side_effect=RuntimeError("network down")):
            html = app_mod.home("hi")
        self.assertIn("News unavailable", html)
        self.assertNotIn("Nifty rallies", html)


if __name__ == "__main__":
    unittest.main()
