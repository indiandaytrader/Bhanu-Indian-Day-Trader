"""NIFTY / BANKNIFTY option chain with OI and PCR (NSE India, public API).

Fetches the NSE India option-chain-indices endpoint
(``https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY``) with a
plain ``urllib`` session: first a GET of the NSE homepage to collect cookies,
then the API call with a browser-like User-Agent and Accept headers. The
response is parsed into CE/PE rows per strike (strikePrice, openInterest,
changeinOpenInterest, lastPrice, impliedVolatility), and the module computes
total CE OI, total PE OI, PCR = PE OI / CE OI, the ATM strike (nearest to the
underlying) and max pain (the strike minimising total payout at expiry).

NSE frequently blocks automated requests. When the fetch fails for any
reason (network error, non-200 status, bad JSON, unexpected shape), the
functions return ``{"error": "..."}`` with a clear message — they NEVER
invent or return fake chain data. Callers must check for the ``"error"``
key before using any number.

No API key is needed; this is NSE's public, unauthenticated endpoint.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import urllib.error
import urllib.request
from typing import Optional, Sequence

#: NSE homepage — fetched first so the session holds the cookies the API wants.
NSE_HOME_URL = "https://www.nseindia.com"
#: NSE option-chain endpoint for indices.
NSE_CHAIN_URL = "https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
#: Index symbols the NSE endpoint serves (CLI exposes NIFTY / BANKNIFTY).
SUPPORTED_SYMBOLS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/option-chain",
}

__all__ = ["fetch_chain", "pcr_summary", "demo_chain"]


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def _opener() -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def fetch_chain(symbol: str = "NIFTY", timeout: float = 15.0) -> dict:
    """Fetch the option chain for an index symbol from NSE India.

    Returns a dict with ``symbol``, ``underlying`` (last price),
    ``as_on`` (timestamp string from NSE, if present) and ``strikes`` — a
    list of per-strike dicts, each with ``strike``, ``ce`` and ``pe``
    sub-dicts (``oi``, ``oi_change``, ``last_price``, ``iv``; ``None`` when
    that side has no quote). Also includes ``total_ce_oi``,
    ``total_pe_oi``, ``pcr``, ``atm_strike`` and ``max_pain``.

    On ANY failure returns ``{"error": "..."}`` — never fake data.
    """
    symbol = str(symbol).upper().strip()
    if symbol not in SUPPORTED_SYMBOLS:
        return {"error": f"unsupported symbol {symbol!r}; use one of {', '.join(SUPPORTED_SYMBOLS)}"}

    opener = _opener()
    try:
        home_req = urllib.request.Request(NSE_HOME_URL, headers=_HEADERS)
        with opener.open(home_req, timeout=timeout) as resp:
            resp.read()
        api_req = urllib.request.Request(
            NSE_CHAIN_URL.format(symbol=symbol), headers=_HEADERS
        )
        with opener.open(api_req, timeout=timeout) as resp:
            if resp.status != 200:
                return {"error": f"NSE option-chain API returned HTTP {resp.status} for {symbol}"}
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        return {"error": f"NSE blocked the request (HTTP {exc.code}) for {symbol} — try again later"}
    except urllib.error.URLError as exc:
        return {"error": f"could not reach NSE ({exc.reason}) — check the network and try again"}
    except (TimeoutError, OSError) as exc:
        return {"error": f"could not reach NSE ({exc}) — check the network and try again"}

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"error": "NSE returned a response that is not valid JSON — try again later"}

    records = payload.get("records")
    if not isinstance(records, dict) or not isinstance(records.get("data"), list):
        return {"error": "NSE returned an unexpected option-chain format — try again later"}
    data = records["data"]
    if not data:
        return {"error": f"NSE returned an empty option chain for {symbol}"}

    underlying = records.get("underlyingValue")
    strikes: list[dict] = []
    for row in data:
        strike = row.get("strikePrice")
        if strike is None:
            continue
        strikes.append(
            {
                "strike": float(strike),
                "ce": _parse_side(row.get("CE")),
                "pe": _parse_side(row.get("PE")),
            }
        )
    if not strikes:
        return {"error": f"NSE returned no usable strikes for {symbol}"}
    strikes.sort(key=lambda s: s["strike"])

    total_ce_oi = sum((s["ce"] or {}).get("oi") or 0 for s in strikes)
    total_pe_oi = sum((s["pe"] or {}).get("oi") or 0 for s in strikes)
    pcr = (total_pe_oi / total_ce_oi) if total_ce_oi else None
    atm = min(strikes, key=lambda s: abs(s["strike"] - float(underlying)))["strike"] if underlying else None

    return {
        "symbol": symbol,
        "underlying": float(underlying) if underlying is not None else None,
        "as_on": records.get("timestamp"),
        "strikes": strikes,
        "total_ce_oi": total_ce_oi,
        "total_pe_oi": total_pe_oi,
        "pcr": pcr,
        "atm_strike": atm,
        "max_pain": _max_pain(strikes),
    }


def _parse_side(side: Optional[dict]) -> Optional[dict]:
    """One CE/PE side of a strike row, or None when the side is absent."""
    if not isinstance(side, dict):
        return None
    return {
        "oi": int(side.get("openInterest") or 0),
        "oi_change": int(side.get("changeinOpenInterest") or 0),
        "last_price": float(side["lastPrice"]) if side.get("lastPrice") is not None else None,
        "iv": float(side["impliedVolatility"]) if side.get("impliedVolatility") is not None else None,
    }


def _max_pain(strikes: Sequence[dict]) -> Optional[float]:
    """Strike minimising total payout if the underlying expired there.

    Payout at strike S = sum over CE of max(0, S - K) * CE_OI
                       + sum over PE of max(0, K - S) * PE_OI.
    """
    best_strike: Optional[float] = None
    best_payout: Optional[float] = None
    for candidate in strikes:
        s = candidate["strike"]
        payout = 0.0
        for row in strikes:
            k = row["strike"]
            ce_oi = (row["ce"] or {}).get("oi") or 0
            pe_oi = (row["pe"] or {}).get("oi") or 0
            if s > k:
                payout += (s - k) * ce_oi
            if k > s:
                payout += (k - s) * pe_oi
        if best_payout is None or payout < best_payout:
            best_payout = payout
            best_strike = s
    return best_strike


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def pcr_summary(chain: dict) -> dict:
    """Compact summary of a chain dict from ``fetch_chain`` (or ``demo_chain``).

    Returns ``symbol``, ``underlying``, ``pcr``, ``total_ce_oi``,
    ``total_pe_oi``, ``atm_strike``, ``max_pain``, ``top_ce_oi_strike`` and
    ``top_pe_oi_strike`` (strikes with the highest OI on each side), plus a
    short ``pcr_read``: PCR > 1 means PE OI exceeds CE OI (more put writing
    / hedging — often read as bullish), PCR < 1 the reverse. It is a
    sentiment gauge, not a signal on its own.

    If ``chain`` carries an ``"error"``, the error is passed through
    unchanged (``{"error": ...}``).
    """
    if "error" in chain:
        return {"error": chain["error"]}
    strikes = chain.get("strikes") or []

    def _top(side: str) -> Optional[float]:
        best: Optional[dict] = None
        for row in strikes:
            entry = row.get(side) or {}
            if best is None or (entry.get("oi") or 0) > ((best.get(side) or {}).get("oi") or 0):
                best = row
        return best["strike"] if best is not None else None

    pcr = chain.get("pcr")
    if pcr is None:
        read = "PCR unavailable (no CE OI)"
    elif pcr > 1:
        read = "PCR > 1: PE OI exceeds CE OI (put-heavy — often read as bullish)"
    elif pcr < 1:
        read = "PCR < 1: CE OI exceeds PE OI (call-heavy — often read as bearish)"
    else:
        read = "PCR = 1: CE and PE OI are balanced"
    return {
        "symbol": chain.get("symbol"),
        "underlying": chain.get("underlying"),
        "as_on": chain.get("as_on"),
        "pcr": pcr,
        "total_ce_oi": chain.get("total_ce_oi"),
        "total_pe_oi": chain.get("total_pe_oi"),
        "atm_strike": chain.get("atm_strike"),
        "max_pain": chain.get("max_pain"),
        "top_ce_oi_strike": _top("ce"),
        "top_pe_oi_strike": _top("pe"),
        "pcr_read": read,
    }


# ---------------------------------------------------------------------------
# Offline demo chain (synthetic — NOT real NSE data)
# ---------------------------------------------------------------------------

def demo_chain(symbol: str = "NIFTY") -> dict:
    """A small synthetic chain in ``fetch_chain`` format — offline demo only.

    NOT real NSE data. Lets the summary/CLI formatting be exercised with
    no network.
    """
    strikes: list[dict] = []
    # Synthetic OI hump centred near 25800, PE OI slightly heavier.
    for k in range(25_400, 26_201, 50):
        distance = abs(k - 25_800)
        ce_oi = max(0, int(120_000 - distance * 220 - (k - 25_800) * 40))
        pe_oi = max(0, int(135_000 - distance * 200 + (k - 25_800) * 40))
        strikes.append(
            {
                "strike": float(k),
                "ce": {"oi": ce_oi, "oi_change": 1_200, "last_price": max(1.0, (25_800 - k) * 0.4 + 120.0), "iv": 11.5},
                "pe": {"oi": pe_oi, "oi_change": -800, "last_price": max(1.0, (k - 25_800) * 0.4 + 110.0), "iv": 12.0},
            }
        )
    total_ce = sum(s["ce"]["oi"] for s in strikes)
    total_pe = sum(s["pe"]["oi"] for s in strikes)
    return {
        "symbol": symbol,
        "underlying": 25_812.35,
        "as_on": "demo (synthetic data — NOT real NSE data)",
        "strikes": strikes,
        "total_ce_oi": total_ce,
        "total_pe_oi": total_pe,
        "pcr": total_pe / total_ce,
        "atm_strike": 25_800.0,
        "max_pain": _max_pain(strikes),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m bhanu.options",
        description="NIFTY / BANKNIFTY option chain with OI and PCR (NSE India public API).",
        epilog="If NSE blocks the request, the command reports the error — it never shows fake data.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    chain = sub.add_parser("chain", help="print ATM, PCR and top OI strikes")
    chain.add_argument(
        "--symbol", choices=["NIFTY", "BANKNIFTY"], default="NIFTY",
        help="index symbol (default: NIFTY)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "chain":
        chain = fetch_chain(args.symbol)
        if "error" in chain:
            print(f"Option chain unavailable: {chain['error']}", file=sys.stderr)
            return 2
        summary = pcr_summary(chain)
        print(f"{summary['symbol']} option chain (as on: {summary['as_on']})")
        print(f"Underlying: {summary['underlying']}")
        print(f"ATM strike: {summary['atm_strike']}")
        print(f"Max pain:   {summary['max_pain']}")
        print(f"Total CE OI: {summary['total_ce_oi']:,}")
        print(f"Total PE OI: {summary['total_pe_oi']:,}")
        print(f"PCR: {summary['pcr']:.2f} — {summary['pcr_read']}")
        print(f"Top CE OI strike: {summary['top_ce_oi_strike']}")
        print(f"Top PE OI strike: {summary['top_pe_oi_strike']}")
        return 0
    return 2


def demo() -> None:
    """Offline demo on a synthetic chain (no network, NOT real NSE data)."""
    summary = pcr_summary(demo_chain())
    print("Option chain demo — synthetic data only (NOT real NSE data).")
    print(f"{summary['symbol']}: underlying {summary['underlying']}, "
          f"ATM {summary['atm_strike']}, max pain {summary['max_pain']}")
    print(f"Total CE OI: {summary['total_ce_oi']:,}, "
          f"total PE OI: {summary['total_pe_oi']:,}, PCR: {summary['pcr']:.2f}")
    print(f"Top CE OI strike: {summary['top_ce_oi_strike']}, "
          f"top PE OI strike: {summary['top_pe_oi_strike']}")
    print(summary["pcr_read"])


if __name__ == "__main__":
    if len(sys.argv) == 1:
        demo()
    else:
        raise SystemExit(main())
