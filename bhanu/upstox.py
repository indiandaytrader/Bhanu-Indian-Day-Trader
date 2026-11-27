# bhanu/upstox.py
"""Optional read-only option-chain fallback. Credentials stay in the process."""
import json
import os
from datetime import date, timedelta
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from bhanu.options import parse_chain

INSTRUMENTS = {"NIFTY": "NSE_INDEX|Nifty 50", "BANKNIFTY": "NSE_INDEX|Nifty Bank"}

# Cloudflare (in front of api.upstox.com) rejects the default Python-urllib
# user-agent with HTTP 403 / error 1010. A browser-like UA is required.
HEADERS = {"Accept": "application/json",
           "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) BhanuIndianDayTrader/1.0"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _get_json(url, token):
    req = Request(url, headers=dict(HEADERS, Authorization="Bearer " + token))
    with build_opener(NoRedirect()).open(req, timeout=8) as response:
        return json.load(response)


def _fallback_expiry(symbol):
    """Nearest expiry by calendar: NIFTY weekly = Tuesday, BANKNIFTY = last Thursday."""
    today = date.today()
    if symbol == "NIFTY":
        days_ahead = (1 - today.weekday()) % 7  # Tuesday is weekday 1
        return today + timedelta(days=days_ahead)
    # Last Thursday of the current month; if passed, next month's.
    year, month = today.year, today.month
    for _ in range(2):
        if month == 12:
            nxt = date(year + 1, 1, 1)
        else:
            nxt = date(year, month + 1, 1)
        last_day = nxt - timedelta(days=1)
        thursday = last_day - timedelta(days=(last_day.weekday() - 3) % 7)
        if thursday >= today:
            return thursday
        month = month + 1 if month < 12 else 1
        year = year + 1 if month == 1 else year
    return today


def _nearest_expiry(instrument, symbol, token):
    """Nearest listed expiry on/after today from the option-contract endpoint."""
    try:
        payload = _get_json(
            "https://api.upstox.com/v2/option/contract?instrument_key="
            + instrument.replace("|", "%7C").replace(" ", "%20"), token)
        if payload.get("status") != "success" or not isinstance(payload.get("data"), list):
            raise ValueError
        today = date.today()
        expiries = sorted({date.fromisoformat(row["expiry"])
                           for row in payload["data"] if row.get("expiry")})
        for expiry in expiries:
            if expiry >= today:
                return expiry
        raise ValueError
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return _fallback_expiry(symbol)


def fetch(symbol):
    token = os.environ.get("UPSTOX_ACCESS_TOKEN")
    if not token:
        return {"error": "Upstox access token unavailable"}
    instrument = INSTRUMENTS[symbol]
    expiry = _nearest_expiry(instrument, symbol, token)
    params = urlencode({"instrument_key": instrument,
                        "expiry_date": expiry.isoformat()})
    try:
        payload = _get_json(
            "https://api.upstox.com/v2/option/chain?" + params, token)
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
