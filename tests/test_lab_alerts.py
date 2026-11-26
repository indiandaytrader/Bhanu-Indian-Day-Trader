# tests/test_lab_alerts.py
import unittest
from bhanu.range_replay import replay


class LabTests(unittest.TestCase):
    def test_strategy_windows_no_lookahead_and_drawdown(self):
        rows = ["time,open,high,low,close"]
        for day in ["2026-01-02","2026-02-02"]:
            for m in range(555,915,5):
                bar = "100,101,99,100"
                if m == 555:
                    bar = "100,115,99,100"
                if m == 600:
                    bar = "100,106,99,105"
                rows.append(f"{day}T{m//60:02}:{m%60:02},{bar}")
        args = ("\n".join(rows),"2026-01-01","2026-03-01",1,0,1)
        self.assertFalse(replay(*args,"orb30")["trades"])
        r = replay(*args,"channel")
        self.assertEqual(len(r["trades"]),2)
        self.assertEqual(r["max_drawdown"],14)
        self.assertEqual(r["trades"][-1]["equity"],-14)
        with self.assertRaises(ValueError):
            replay(*args,"undefined")


class SnapshotAlertsTests(unittest.TestCase):
    def test_fresh_levels_and_independent_news_and_risk(self):
        from datetime import datetime
        from bhanu.webhooks import IST, snapshot_alerts
        now = datetime(2026,11,26,11,tzinfo=IST)
        rows = [{"date":"2026-11-26","symbol":"NIFTY","nearest_support":99.95}]
        ticks = {"NIFTY":{"ltp":100,"time":now.timestamp()}}
        news = [{"id":"abc","title":"Earnings released"}]
        events = dict(snapshot_alerts(now,rows,ticks,news,1500,1500))
        self.assertEqual(len(events),3)
        ticks["NIFTY"]["time"] -= 130
        self.assertEqual(list(dict(snapshot_alerts(now,rows,ticks,news,0,1500))),["news/abc"])
