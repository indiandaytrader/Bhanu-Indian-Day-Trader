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
from bhanu import paper as paper_mod
from bhanu import risk as risk_mod
from bhanu import scanner as scanner_mod
from bhanu import sizing as sizing_mod
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


if __name__ == "__main__":
    unittest.main()
