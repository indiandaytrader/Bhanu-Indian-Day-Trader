"""tests/test_chain_terminal.py — option-chain and feed regression tests."""
import unittest
import urllib.error
from unittest.mock import patch

from bhanu import options, poller
from web import app


def contract(strike, expiry, calls, puts):
    row = {"strikePrice": strike, "expiryDate": expiry}
    for key, oi in (("CE", calls), ("PE", puts)):
        if oi is not None:
            row[key] = {"openInterest": oi, "lastPrice": 45.25, "impliedVolatility": 12.75}
    return row


class ChainTests(unittest.TestCase):
    def setUp(self):
        self.rows = [contract(i, "03-Dec-2026", 20, 10) for i in range(50, 160)]
        self.rows += [contract(120, "10-Dec-2026", 0, 700),
                      contract(161, "03-Dec-2026", None, 0)]

    def parse(self):
        return options.parse_chain("NIFTY", {"records": {"underlyingValue": 120,
                                   "timestamp": "exchange time", "data": self.rows}})

    def test_expiries_and_oi(self):
        chain = self.parse()
        self.assertEqual(chain["expiry"], "2026-12-03")
        self.assertEqual(chain["pcr"], 0.5)
        self.assertEqual(chain["total_pe_oi"], 1100)
        self.assertIsNone(chain["expiries"]["2026-12-10"]["pcr"])
        self.assertEqual(chain["max_pain"], 86)

    def test_missing_oi_is_unknown(self):
        del self.rows[0]["CE"]["openInterest"]
        chain = self.parse()
        self.assertIsNone(chain["pcr"])
        self.assertIsNone(chain["max_pain"])
        self.assertIsNone(chain["total_ce_oi"])

    def test_cache_to_render(self):
        with patch.object(poller, "STATE", poller._fresh_state()), patch.object(
            options, "fetch_chain", side_effect=[self.parse()] * 2 + [{"error": "HTTP 403"}] * 2
        ) as fetch:
            self.assertIn("Waiting for", app.chain_page("en", {}))
            poller._tick_chains()
            poller._tick_chains()
            html = app.chain_page("hi", {})
            self.assertIn("120 · ATM", html)
            self.assertIn("45.25", html)
            self.assertIn("12.75", html)
            self.assertIn("HTTP 403", html)
            self.assertIn("Last good snapshot", html)
            self.assertIn("21 of 111", html)
            second = app.chain_page("en", {"expiry": ["2026-12-10"]})
            self.assertIn("1 of 1", second)
            self.assertEqual(fetch.call_count, 4)

    def test_retry_status_survives_real_fetch_path(self):
        error = urllib.error.HTTPError("url", 429, "limited", {}, None)
        with patch.object(poller, "STATE", poller._fresh_state()), patch(
            "urllib.request.urlopen", side_effect=error
        ) as requests:
            self.assertFalse(poller._do_spark(["^NSEI"]))
            self.assertTrue(poller.status()["backoff_until"])
            self.assertFalse(poller._do_spark(["^NSEI"]))
            self.assertEqual(requests.call_count, 2)

    def test_cold_dashboard_reads(self):
        state = poller._fresh_state()
        state["running"] = True
        with patch.object(poller, "STATE", state), patch.object(app, "_cached", side_effect=AssertionError):
            for response in (app._api_quotes(), app._api_flow(), app._api_sectors(),
                             app._api_intraday({}), app._api_tmmini(), app._api_levelsmini()):
                self.assertIn("warming", response["error"])
