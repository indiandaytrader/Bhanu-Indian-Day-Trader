# bhanu/upstox.py
"""Optional read-only option-chain fallback. Credentials stay in the process."""
import json
import os
from datetime import date
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from bhanu.options import parse_chain

INSTRUMENTS = {"NIFTY": "NSE_INDEX|Nifty 50", "BANKNIFTY": "NSE_INDEX|Nifty Bank"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(symbol):
    token = os.environ.get("UPSTOX_ACCESS_TOKEN")
    if not token:
        return {"error": "Upstox access token unavailable"}
    instrument = INSTRUMENTS[symbol]
    params = urlencode({"instrument_key": instrument, "expiry_date":
                        "current_week" if symbol == "NIFTY" else "current_month"})
    try:
        req = Request("https://api.upstox.com/v2/option/chain?" + params,
                      headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
        with build_opener(NoRedirect()).open(req, timeout=8) as response:
            payload = json.load(response)
        if payload.get("status") != "success" or not isinstance(payload.get("data"), list):
            raise ValueError
        rows = []
        spot = None
        for row in payload["data"]:
            if row["underlying_key"] != instrument:
                raise ValueError
            if spot is None:
                spot = row.get("underlying_spot_price")
            item = {"strikePrice": row["strike_price"],
                    "expiryDate": date.fromisoformat(row["expiry"]).strftime("%d-%b-%Y")}
            for key, name in (("CE", "call_options"), ("PE", "put_options")):
                leg = row.get(name) or {}
                market = leg.get("market_data") or {}
                if market:
                    item[key] = {"lastPrice": market.get("ltp"), "openInterest": market.get("oi"),
                                 "impliedVolatility": (leg.get("option_greeks") or {}).get("iv")}
            rows.append(item)
        result = parse_chain(symbol, {"records": {"underlyingValue": spot, "data": rows}})
        if "error" in result:
            raise ValueError
        result["source"] = "Upstox"
        return result
    except HTTPError as exc:
        exc.close()
        return {"error": f"Upstox HTTP {exc.code}"}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {"error": "Upstox unavailable or returned an unreadable chain"}
