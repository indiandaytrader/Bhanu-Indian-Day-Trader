# tests/test_advanced.py
import unittest
from bhanu import greeks


class GreeksTests(unittest.TestCase):
    def test_reference_and_bounds(self):
        for call in (True, False):
            result = greeks.solve(100, 100, 7.9655674554, 1, call)
            self.assertAlmostEqual(result["model_iv"], 20, places=6)
            self.assertAlmostEqual(result["delta"], .53982784 - (not call), places=6)
            self.assertAlmostEqual(result["gamma"], .01984763, places=7)
            self.assertAlmostEqual(result["theta"], -.010875, places=5)
            self.assertAlmostEqual(result["vega"], .3969525, places=6)
        self.assertFalse(greeks.solve(120, 100, 19, 1, True))
        self.assertIsNone(greeks.years_left(None, "2026-11-26"))
        self.assertEqual(greeks.years_left("26-Nov-2026 15:30:00", "2026-11-26"), 0)


class RangeTests(unittest.TestCase):
    def test_months_and_costs(self):
        from bhanu.range_replay import replay
        rows = ["time,open,high,low,close"]
        for day in ("2026-01-02", "2026-03-02"):
            for t in range(555,915,5):
                values = "100,101,99,100" if t < 570 else "101,103,100,102"
                rows.append(f"{day}T{t//60:02}:{t%60:02},{values}")
        r = replay("\n".join(rows),"2026-01-01","2026-03-31",2,10,10)
        self.assertEqual(r["complete"], 2)
        self.assertEqual(len(r["trades"]), 2)
        self.assertEqual(r["trades"][0]["exit"], "stop")
        self.assertAlmostEqual(r["trades"][0]["net"], -26.02)


class BrokerTests(unittest.TestCase):
    def test_fallback_only_with_key_and_error(self):
        from unittest.mock import patch
        from bhanu import poller
        with patch.object(poller, "STATE", poller._fresh_state()), patch.object(
                poller.options_mod, "fetch_chain", return_value={"error": "403"}), patch.object(
                poller.upstox, "fetch", return_value={"source": "Upstox"}) as broker:
            with patch.dict("os.environ", {"UPSTOX_ACCESS_TOKEN": ""}):
                poller._tick_chains()
                broker.assert_not_called()
            with patch.dict("os.environ", {"UPSTOX_ACCESS_TOKEN": "test"}):
                poller._tick_chains()
                self.assertEqual(poller.get_chain("NIFTY")["data"]["source"], "Upstox")

    def test_provider_format(self):
        import io, json
        from unittest.mock import patch
        from bhanu import upstox
        row = {"strike_price": 100, "expiry": "2026-12-03", "underlying_spot_price": 101,
               "underlying_key": "NSE_INDEX|Nifty 50",
               "call_options": {"market_data": {"oi": 20, "ltp": 2}, "option_greeks": {"iv": 14}}}
        with patch.dict("os.environ", {"UPSTOX_ACCESS_TOKEN": "test"}), patch.object(upstox, "build_opener") as conn:
            conn.return_value.open.return_value = io.BytesIO(json.dumps({"data":[row],"status":"success"}).encode())
            chain = upstox.fetch("NIFTY")
            self.assertEqual(chain["strikes"][0]["ce"]["iv"], 14)
            self.assertIsNone(chain["as_on"])


class AlertTests(unittest.TestCase):
    def test_close_date_and_delivery_dedup(self):
        import tempfile
        from pathlib import Path
        from datetime import datetime
        from unittest.mock import patch
        from bhanu import webhooks
        hit = {"symbol":"NIFTY","direction":"long","bar_time":"09:30","date":"2026-11-26","close":100}
        def at(h,m):
            return datetime(2026,11,26,h,m,tzinfo=webhooks.IST)
        self.assertFalse(list(webhooks.events(at(9,34),[hit])))
        self.assertFalse(list(webhooks.events(at(10,0),[hit])))
        with tempfile.TemporaryDirectory() as tmp, patch.object(webhooks,"DB",Path(tmp)/"alerts"), patch.dict(
                "os.environ", {"TELEGRAM_WEBHOOK_URL":"","DISCORD_WEBHOOK_URL":"test"}), patch.object(webhooks,"send") as sent:
            for _ in range(2):
                webhooks.deliver(at(9,36), [hit])
                webhooks.deliver(at(15,15), [hit])
            self.assertEqual(sent.call_count, 2)


class DetailTests(unittest.TestCase):
    def test_slow_requests_share_work(self):
        from threading import Event
        from web.detail_jobs import DetailJobs
        jobs, gate = DetailJobs(), Event()
        try:
            def build():
                gate.wait(3)
                return {"value": 7}
            self.assertEqual(jobs.read("x",build)[0],202)
            self.assertEqual(jobs.read("x",build)[0],202)
            gate.set()
            jobs.pool.shutdown()
            self.assertEqual(jobs.read("x",build),(200,{"value":7}))
        finally:
            gate.set()
            jobs.pool.shutdown()
