"""Company results / filings analysis for NSE-listed companies (NSE port).

Port of the DayTrader ``trader-analyst`` rules, rewritten from the study
note. The US original reads SEC XBRL company facts (10-Q/10-K) and
earnings 8-Ks. SEC EDGAR has no Indian equivalent, so this module uses
NSE's public corporate-announcements feed instead, and is honest about
what that feed does and does not contain.

DATA LIMITS (read before trusting any output):
    * Free NSE results data is patchy. The public announcements feed
      lists announcement subjects and dates — it does NOT carry revenue,
      net profit or EPS figures. This module therefore reports
      *availability* and never invents figures.
    * ``latest_results`` returns ``{"available": False, "reason": ...}``
      whenever figures are not actually in hand. A results announcement
      having been published is not the same as having its numbers.
    * ``results_due_inside_trade`` returns None (not False) when the
      next results date is unknown — unknown means "cannot say", and
      must never be read as "no results due".
    * The NSE endpoint is best-effort: it may refuse requests without
      browser-like headers/cookies, rate-limit, or change shape. Any
      failure returns ``{"error": ...}`` with a clear message.
    * Nothing here is a trading signal. The two questions it answers
      are the DayTrader ones: what did the last report say, and is the
      next report inside this trade?

No broker code, no orders, no real-money anything. Network access is
confined to ``fetch_announcements``; every other function works on data
already supplied (or on the offline synthetic fixture in ``__main__``).
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.request
from datetime import date, datetime

NSE_ANNOUNCEMENTS_URL = (
    "https://www.nseindia.com/api/corporate-announcements"
    "?index=equities&symbol={symbol}"
)
# NSE rejects bare urllib requests; a browser-like User-Agent is needed.
# Even with it, the endpoint may 403/429 — that is a normal outcome here.
NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
}
REQUEST_TIMEOUT_SECONDS = 10

RESULTS_KEYWORDS = (
    "financial result",
    "financial results",
    "quarterly result",
    "quarterly results",
    "audited financial",
    "unaudited financial",
)


def fetch_announcements(symbol: str) -> list | dict:
    """Best-effort fetch of NSE corporate announcements for a symbol.

    Args:
        symbol: NSE symbol, e.g. "RELIANCE" (case-insensitive).

    Returns:
        A list of announcement dicts (each with at least ``symbol``,
        ``subject`` and ``date`` keys when the feed provides them), or
        ``{"error": "..."}`` on any failure. Never fakes data: a failed
        fetch is reported as a failure, not as an empty result.
    """
    symbol = symbol.strip().upper()
    if not symbol:
        return {"error": "empty symbol"}
    url = NSE_ANNOUNCEMENTS_URL.format(symbol=symbol)
    request = urllib.request.Request(url, headers=NSE_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {
            "error": f"NSE announcements request failed: HTTP {exc.code} "
            f"for {symbol} (endpoint may need browser session/cookies)"
        }
    except urllib.error.URLError as exc:
        return {"error": f"NSE announcements request failed: {exc.reason}"}
    except (json.JSONDecodeError, UnicodeDecodeError, TimeoutError) as exc:
        return {"error": f"NSE announcements response unreadable: {exc}"}
    except Exception as exc:  # network surprises (DNS, reset, ...)
        return {"error": f"NSE announcements request failed: {exc}"}

    if isinstance(payload, dict) and payload.get("error"):
        return {"error": f"NSE returned an error for {symbol}: {payload['error']}"}
    if not isinstance(payload, list):
        return {"error": f"NSE returned unexpected data for {symbol}"}
    return [
        {
            "symbol": item.get("symbol", symbol),
            "subject": item.get("subject", ""),
            "date": item.get("an_dt", item.get("date", "")),
            "description": item.get("desc", ""),
            "attachment": item.get("attchmntText", ""),
        }
        for item in payload
        if isinstance(item, dict)
    ]


def _is_results_announcement(announcement: dict) -> bool:
    subject = str(announcement.get("subject", "")).lower()
    return any(keyword in subject for keyword in RESULTS_KEYWORDS)


def _parse_date(value: str) -> date | None:
    """Parse an NSE-style date ("26-Nov-2026", "2026-11-26", ...)."""
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def latest_results(symbol: str, announcements: list | dict | None = None) -> dict:
    """Latest reported results for a symbol, if figures are available.

    Args:
        symbol: NSE symbol.
        announcements: Optional pre-fetched list (from
            ``fetch_announcements``); when None, a live fetch is attempted.

    Returns:
        ``{"available": True, "revenue": ..., "net_profit": ...,
        "eps": ..., "announcement_date": ..., "one_offs": ...}`` when
        figures are in hand, else ``{"available": False, "reason": ...}``.
        The NSE announcements feed carries no figures, so a live call
        normally returns ``available: False`` with the date of the most
        recent results announcement (if any) — the fact of the
        announcement is reported, its numbers are not invented.
    """
    if announcements is None:
        announcements = fetch_announcements(symbol)
    if isinstance(announcements, dict) and "error" in announcements:
        return {
            "available": False,
            "reason": f"could not fetch announcements: {announcements['error']}",
        }

    results_items = [a for a in announcements if _is_results_announcement(a)]
    if not results_items:
        return {
            "available": False,
            "reason": f"no financial-results announcement found for {symbol} "
            "in the NSE feed",
        }
    latest = max(
        results_items,
        key=lambda a: _parse_date(str(a.get("date", ""))) or date.min,
    )
    return {
        "available": False,
        "reason": (
            "NSE announcements feed lists the results announcement but "
            "carries no revenue / net profit / EPS figures — figures must "
            "come from the company's published results PDF or a results "
            "database, neither of which this module fabricates"
        ),
        "announcement_date": latest.get("date", ""),
        "announcement_subject": latest.get("subject", ""),
        "revenue": None,
        "net_profit": None,
        "eps": None,
        "one_offs": None,
    }


def results_due_inside_trade(
    symbol: str,
    trade_date: date,
    announcements: list | dict | None = None,
) -> bool | None:
    """Is the next results announcement inside this (intraday) trade?

    An intraday trade lives and dies on one date, so this is True only
    when a results announcement is dated ``trade_date`` itself. The NSE
    announcements feed is backward-looking (what was announced, not what
    is scheduled), so the next results date is usually unknown — in
    that case this returns None and the caller must treat the answer as
    "unknown", never as "no results due".
    """
    if announcements is None:
        announcements = fetch_announcements(symbol)
    if isinstance(announcements, dict) and "error" in announcements:
        return None
    for announcement in announcements:
        if _is_results_announcement(announcement):
            announced = _parse_date(str(announcement.get("date", "")))
            if announced is not None and announced == trade_date:
                return True
    return None  # no scheduled results date is published in this feed


def flag_one_offs(financials: dict) -> bool | None:
    """Flag one-offs: net income above operating income.

    Args:
        financials: Dict with ``net_income`` and ``operating_income``
            (same currency, same period). Missing either value returns
            None — "cannot tell", not "no one-offs".

    Returns:
        True when net income > operating income (non-operating gains
        flatter the headline), False when operating income covers net
        income, None when the inputs are missing.
    """
    net = financials.get("net_income")
    operating = financials.get("operating_income")
    if net is None or operating is None:
        return None
    return float(net) > float(operating)


# --- Offline synthetic fixture (used by the __main__ demo only) --------

SYNTHETIC_ANNOUNCEMENTS = [
    {
        "symbol": "RELIANCE",
        "subject": "Board Meeting - Financial Results for quarter ended Sep 2026",
        "date": "24-Oct-2026",
        "description": "Quarterly results (figures not in the feed).",
        "attachment": "",
    },
    {
        "symbol": "RELIANCE",
        "subject": "Disclosure under Regulation 30 - New order won",
        "date": "10-Nov-2026",
        "description": "Order win announcement.",
        "attachment": "",
    },
    {
        "symbol": "RELIANCE",
        "subject": "Financial Results for quarter ended Sep 2026",
        "date": "24-Oct-2026",
        "description": "Audited standalone results (figures not in the feed).",
        "attachment": "",
    },
]


def _demo() -> None:
    """Offline demo on the synthetic fixture — no network needed."""
    print("synthetic announcements for RELIANCE:")
    for item in SYNTHETIC_ANNOUNCEMENTS:
        print(f"  {item['date']}  {item['subject']}")

    results = latest_results("RELIANCE", SYNTHETIC_ANNOUNCEMENTS)
    print("\nlatest_results:", json.dumps(results, indent=2))

    trade_day = date(2026, 11, 26)
    due = results_due_inside_trade(
        "RELIANCE", trade_day, SYNTHETIC_ANNOUNCEMENTS
    )
    print(f"\nresults_due_inside_trade({trade_day}): {due} "
          "(None = next results date unknown, not 'no results due')")

    print("\nflag_one_offs (net 120 > operating 100):",
          flag_one_offs({"net_income": 120.0, "operating_income": 100.0}))
    print("flag_one_offs (net 90 <= operating 100):",
          flag_one_offs({"net_income": 90.0, "operating_income": 100.0}))
    print("flag_one_offs (missing inputs):",
          flag_one_offs({"net_income": 120.0}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="NSE analyst demo (offline, synthetic fixture)."
    )
    parser.add_argument("--symbol", default="RELIANCE")
    parser.add_argument("--live", action="store_true",
                        help="attempt a live NSE fetch (needs network)")
    args = parser.parse_args()
    if args.live:
        print(json.dumps(fetch_announcements(args.symbol), indent=2)[:2000])
    else:
        _demo()
