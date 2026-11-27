"""News tracker for NSE day trading.

Sources (best effort, public, unofficial):
    * PRIMARY — NSE / BSE exchange announcements (corporate
      announcements and results). These are the exchange's own record
      of what a listed company disclosed.
    * SECONDARY — Google News RSS per symbol (live, no key): the
      fresh headlines. Yahoo India headlines RSS is kept as a
      fallback, but it often serves days-old items.
    * TERTIARY — Marketaux (optional, needs a free API key in
      ``MARKETAUX_API_KEY``): global financial news with per-entity
      sentiment scores (-1..+1). Free plan: 100 requests/day, 3
      articles/request. Fetched at most once per symbol per hour and
      only for the 3 indices or symbols moving >= 3% — never the
      whole 39-symbol universe — with a 90-request daily cap and a
      60-minute backoff after HTTP 429.

There is no SEC / EDGAR equivalent in India and none is used here.

Rules (read these before acting on anything this module returns):
    * Announcements beat headlines. An exchange announcement is a
      company disclosure; a headline is a journalist's summary of it.
    * Headlines are leads, not facts. A headline says "something may
      have happened" — verify it against the announcement or the
      company's own filing before believing it.
    * Never trade on a headline alone. News is a filter (skip or
      shrink a trade when the news is against you), never an entry
      signal by itself.
    * MOVING = the symbol moved >= 3% from its previous close.

Dedupe: seen items are kept in ``data/news_state.json`` for 30 days so
the same announcement is not reported twice.

Public entry point: :func:`check_symbol`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = REPO_ROOT / "data" / "news_state.json"
STATE_TTL = timedelta(days=30)

USER_AGENT = "Mozilla/5.0 (bhanu-indian-day-trader; paper-trading research)"
REQUEST_TIMEOUT_S = 20

#: A symbol is MOVING when |change vs previous close| reaches this %.
MOVING_PCT = 3.0

# ---------------------------------------------------------------------------
# Tagging
# ---------------------------------------------------------------------------

#: Keyword rules per tag. First match wins per tag; an item can carry
#: several tags. Word-boundary matching keeps "deal" out of "dealing".
TAG_KEYWORDS: dict[str, tuple[str, ...]] = {
    "earnings": (
        "results", "quarterly", "q1", "q2", "q3", "q4", "fy26", "fy25",
        "earnings", "profit", "net profit", "revenue", "dividend",
        "buyback", "bonus issue", "stock split",
    ),
    "deal": (
        "merger", "acquisition", "acquire", "takeover", "stake",
        "bulk deal", "block deal", "order win", "contract win",
        "joint venture", "mou", "divestment", "sell-off",
    ),
    "halt": (
        "trading halt", "suspended", "circuit", "upper circuit",
        "lower circuit", "delist",
    ),
    "legal": (
        "court", "lawsuit", "litigation", "sebi", "penalty", "fine",
        "investigation", "fraud", "scam", "raid", "arrest",
    ),
    "regulatory": (
        "rbi", "sebi", "regulation", "approval", "licence", "license",
        "ban", "restriction", "compliance", "show cause",
    ),
    "macro": (
        "rbi rate", "repo rate", "inflation", "gdp", "budget",
        "monsoon", "crude", "rupee", "dollar", "fii", "fpi",
        "election", "tariff",
    ),
}


def tag_text(text: str) -> list[str]:
    """Tag a headline/announcement title by keyword. Returns tag names."""
    lowered = f" {text.lower()} "
    tags: list[str] = []
    for tag, keywords in TAG_KEYWORDS.items():
        for keyword in keywords:
            pattern = r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])"
            if re.search(pattern, lowered):
                tags.append(tag)
                break
    return tags


# ---------------------------------------------------------------------------
# Dedupe state
# ---------------------------------------------------------------------------

def _load_state() -> dict[str, Any]:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        if not isinstance(state, dict):
            return {"seen": {}}
    except (OSError, json.JSONDecodeError):
        return {"seen": {}}
    # Prune entries older than 30 days.
    cutoff = (datetime.now(timezone.utc) - STATE_TTL).isoformat()
    seen = state.get("seen", {})
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    return state


def _save_state(state: dict[str, Any]) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
    except OSError:
        pass  # Dedupe failure must never break a news check.


def _item_id(source: str, title: str, link: str) -> str:
    raw = f"{source}|{title.strip()}|{link.strip()}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Fetchers (best effort — every failure is reported, never hidden)
# ---------------------------------------------------------------------------

def _http_get(url: str, headers: dict[str, str] | None = None) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
        return resp.read()


def fetch_nse_announcements(symbol: str) -> tuple[list[dict[str, Any]], str]:
    """Corporate announcements for ``symbol`` from NSE (best effort).

    NSE's public API needs a session cookie from the homepage first.
    Returns ``(items, note)``; ``note`` explains a failure or an empty
    result. Items have keys: source, title, link, published, tags.
    """
    display = symbol.replace(".NS", "").upper()
    try:
        opener = urllib.request.build_opener()
        opener.addheaders = [
            ("User-Agent", USER_AGENT),
            ("Accept", "application/json, text/plain, */*"),
            ("Accept-Language", "en-IN,en;q=0.9"),
            ("Referer", "https://www.nseindia.com/"),
        ]
        # Session cookie from the homepage, then the announcements API.
        with opener.open("https://www.nseindia.com", timeout=REQUEST_TIMEOUT_S):
            pass
        url = ("https://www.nseindia.com/api/corporate-announcements"
               "?index=equities")
        with opener.open(url, timeout=REQUEST_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - best effort, report it
        return [], f"NSE announcements unavailable: {exc}"
    items: list[dict[str, Any]] = []
    for row in payload if isinstance(payload, list) else []:
        try:
            if str(row.get("symbol", "")).upper() != display:
                continue
            title = str(row.get("subject") or row.get("desc") or "").strip()
            if not title:
                continue
            items.append(
                {
                    "source": "NSE announcement",
                    "title": title,
                    "link": "https://www.nseindia.com/companies-listing/"
                            "corporate-filings-announcements",
                    "published": str(row.get("an_dt") or ""),
                    "tags": tag_text(title),
                    "sentiment": None,
                }
            )
        except (AttributeError, TypeError):
            continue
    note = "" if items else "no NSE announcements found for this symbol"
    return items, note


def fetch_bse_announcements(symbol: str) -> tuple[list[dict[str, Any]], str]:
    """Corporate announcements for ``symbol`` from BSE (best effort).

    BSE publishes announcements per scrip; without a scrip code the
    lookup cannot be scoped, so this returns an empty list with a note
    explaining that the BSE check needs the scrip code. The NSE feed
    above is the primary announcement source.
    """
    return [], (
        "BSE announcements need a BSE scrip code (not a Yahoo symbol); "
        "check bseindia.com for this symbol's announcements directly"
    )


def fetch_yahoo_search_news(symbol: str) -> tuple[list[dict[str, Any]], str]:
    """Headlines for ``symbol`` from Yahoo's search API (RSS fallback).

    The per-symbol RSS feed often returns an empty channel for Indian
    symbols; the search API (v1/finance/search) still returns real
    headlines. Returns ``(items, note)`` like the other fetchers.
    Items have keys: source, title, link, published, tags.
    """
    query = symbol.replace(".NS", "").upper()
    url = (f"https://query1.finance.yahoo.com/v1/finance/search"
           f"?q={query}&newsCount=5&quotesCount=0")
    try:
        raw = _http_get(url)
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - best effort, report it
        return [], f"Yahoo search news unavailable: {exc}"
    items: list[dict[str, Any]] = []
    for row in payload.get("news", []) if isinstance(payload, dict) else []:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        published = ""
        ts = row.get("providerPublishTime")
        if ts:
            try:
                published = datetime.fromtimestamp(
                    int(ts), tz=timezone.utc
                ).strftime("%d %b %Y %H:%M UTC")
            except (TypeError, ValueError):
                published = ""
        items.append(
            {
                "source": "Yahoo headline",
                "title": title,
                "link": str(row.get("link") or ""),
                "published": published,
                "tags": tag_text(title),
                "sentiment": None,
            }
        )
    note = "" if items else "no Yahoo search news returned for this symbol"
    return items, note


def fetch_yahoo_headlines(symbol: str) -> tuple[list[dict[str, Any]], str]:
    """Headline RSS for ``symbol`` from Yahoo Finance India (best effort).

    Falls back to the Yahoo search API when the RSS channel is empty
    (common for Indian symbols). Returns ``(items, note)``.
    """
    yahoo_symbol = symbol if "." in symbol else f"{symbol}.NS"
    url = f"https://finance.yahoo.com/rss/headline?s={yahoo_symbol}"
    try:
        raw = _http_get(url)
        root = ET.fromstring(raw)
    except Exception as exc:  # noqa: BLE001 - best effort, try the fallback
        rss_note = f"Yahoo headlines RSS unavailable: {exc}"
        items, search_note = fetch_yahoo_search_news(symbol)
        if items:
            return items, ""
        return [], f"{rss_note}; {search_note}"
    items: list[dict[str, Any]] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        if not title:
            continue
        items.append(
            {
                "source": "Yahoo headline",
                "title": title,
                "link": link,
                "published": pub_date,
                "tags": tag_text(title),
                "sentiment": None,
            }
        )
    if items:
        return items, ""
    # RSS returned an empty channel — fall back to the search API.
    search_items, search_note = fetch_yahoo_search_news(symbol)
    if search_items:
        return search_items, ""
    return [], (
        "no Yahoo headlines returned for this symbol; "
        f"search API: {search_note}"
    )


def fetch_google_news(symbol: str) -> tuple[list[dict[str, Any]], str]:
    """Live headlines for ``symbol`` from Google News RSS (free, no key).

    Yahoo's India headline RSS often serves days-old items, so Google
    News RSS is the live source: it returns genuinely recent headlines
    (last few hours) with no API key. Returns ``(items, note)``.
    """
    display = symbol.replace(".NS", "").upper()
    query = {
        "NIFTY": "Nifty 50",
        "BANKNIFTY": "Bank Nifty",
        "SENSEX": "Sensex",
    }.get(display, display)
    url = (
        "https://news.google.com/rss/search?q="
        + urllib.parse.quote(query)
        + "&hl=en-IN&gl=IN&ceid=IN:en"
    )
    try:
        raw = _http_get(url)
        root = ET.fromstring(raw)
    except Exception as exc:  # noqa: BLE001 - best effort
        return [], f"Google News RSS unavailable: {exc}"
    items: list[dict[str, Any]] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        src_el = item.find("source")
        src = (src_el.text or "").strip() if src_el is not None else ""
        if not title:
            continue
        items.append(
            {
                "source": "Google News" + (f" · {src}" if src else ""),
                "title": title,
                "link": link,
                "published": pub_date,
                "tags": tag_text(title),
                "sentiment": None,
            }
        )
    if items:
        # Newest first — the RSS order is a relevance mix, so sort by
        # published date so today's headlines sit at the top.
        def _pub_ts(it: dict[str, Any]) -> float:
            try:
                from email.utils import parsedate_to_datetime

                dt = parsedate_to_datetime(it.get("published") or "")
                return dt.timestamp() if dt is not None else 0.0
            except Exception:  # noqa: BLE001 - keep original order
                return 0.0

        items.sort(key=_pub_ts, reverse=True)
        return items, ""
    return [], "no Google News items returned for this symbol"


# ---------------------------------------------------------------------------
# Marketaux (optional, key-gated — extra news + per-entity sentiment)
# ---------------------------------------------------------------------------

MARKETAUX_API_URL = "https://api.marketaux.com/v1/news/all"
#: Free plan: 100 requests/day, 3 articles/request. One fetch per
#: symbol per hour keeps 3 index symbols at 72 requests/day max —
#: a 15-minute cache would need 288/day and blow the free cap.
MARKETAUX_CACHE_S = 3600
MARKETAUX_LIMIT = 3
MARKETAUX_DAILY_CAP = 90  # stop below the 100/day free cap
MARKETAUX_BACKOFF_S = 3600  # 60 min after an HTTP 429

#: Indices have no Marketaux entity symbol — search by index name.
MARKETAUX_INDEX_SEARCH = {
    "NIFTY": "NIFTY 50",
    "BANKNIFTY": "Bank Nifty",
    "SENSEX": "Sensex",
}

_MARKETAUX_CACHE: dict[str, tuple[float, list[dict[str, Any]], str]] = {}
_MARKETAUX_BACKOFF_UNTIL = 0.0
_MARKETAUX_DAY = ""
_MARKETAUX_COUNT = 0


def _marketaux_api_key(api_key: str | None = None) -> str:
    """API key from the argument or the MARKETAUX_API_KEY env var."""
    if api_key:
        return api_key
    return os.environ.get("MARKETAUX_API_KEY", "").strip()


def _marketaux_pick_entity(
    article: dict[str, Any], symbol: str
) -> dict[str, Any] | None:
    """Best entity for ``symbol`` in a Marketaux article.

    The entity whose symbol matches ours wins; otherwise the highest
    match_score entity. Returns None when the article has no entities.
    """
    entities = article.get("entities") or []
    if not isinstance(entities, list) or not entities:
        return None
    wanted = symbol.replace(".NS", "").upper()
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        if str(ent.get("symbol", "")).replace(".NS", "").upper() == wanted:
            return ent
    best: dict[str, Any] | None = None
    best_score = -1.0
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        try:
            score = float(ent.get("match_score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        if score > best_score:
            best, best_score = ent, score
    return best


def _marketaux_published(raw: str) -> str:
    """Marketaux ISO timestamp -> our "%d %b %Y %H:%M UTC" display."""
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    except ValueError:
        return str(raw)


def fetch_marketaux_news(
    symbol: str, api_key: str | None = None
) -> tuple[list[dict[str, Any]], str]:
    """News + entity sentiment for ``symbol`` from Marketaux.

    The key comes from ``api_key`` or the MARKETAUX_API_KEY env var —
    it is never hardcoded, logged, or written to a file. Without a key
    this returns ([], "Marketaux: no API key configured") and no HTTP
    call is made. Results are cached 60 minutes per symbol; HTTP 429
    sets a 60-minute backoff; a 90-request daily cap stops fetches
    before the free plan's 100/day limit. Items carry a numeric
    ``sentiment`` (-1..+1) from the matched entity, or None when
    Marketaux scored nothing for the symbol.
    """
    global _MARKETAUX_BACKOFF_UNTIL, _MARKETAUX_DAY, _MARKETAUX_COUNT
    key = _marketaux_api_key(api_key)
    if not key:
        return [], "Marketaux: no API key configured"
    cache_key = symbol.replace(".NS", "").upper()
    now = time.time()
    cached = _MARKETAUX_CACHE.get(cache_key)
    if cached and now - cached[0] < MARKETAUX_CACHE_S:
        return cached[1], cached[2]
    if now < _MARKETAUX_BACKOFF_UNTIL:
        until = datetime.fromtimestamp(
            _MARKETAUX_BACKOFF_UNTIL, tz=timezone.utc
        ).strftime("%H:%M UTC")
        return [], f"Marketaux: rate limited — backing off until {until}"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _MARKETAUX_DAY != today:
        _MARKETAUX_DAY, _MARKETAUX_COUNT = today, 0
    if _MARKETAUX_COUNT >= MARKETAUX_DAILY_CAP:
        return [], (
            f"Marketaux: daily free-tier limit reached "
            f"({MARKETAUX_DAILY_CAP}/100) — resumes tomorrow"
        )
    params: dict[str, str] = {
        "api_token": key,
        "language": "en",
        "limit": str(MARKETAUX_LIMIT),
        "countries": "in",
    }
    if cache_key in MARKETAUX_INDEX_SEARCH:
        params["search"] = MARKETAUX_INDEX_SEARCH[cache_key]
    else:
        params["symbols"] = cache_key
        params["filter_entities"] = "true"
    # The URL carries the key — never log it.
    url = MARKETAUX_API_URL + "?" + urllib.parse.urlencode(params)
    _MARKETAUX_COUNT += 1
    try:
        raw = _http_get(url)
        payload = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            _MARKETAUX_BACKOFF_UNTIL = now + MARKETAUX_BACKOFF_S
            note = "Marketaux: rate limited (HTTP 429) — backing off 60 min"
        else:
            note = f"Marketaux unavailable: HTTP Error {exc.code}"
        _MARKETAUX_CACHE[cache_key] = (now, [], note)
        return [], note
    except Exception as exc:  # noqa: BLE001 - best effort, report it
        note = f"Marketaux unavailable: {exc}"
        _MARKETAUX_CACHE[cache_key] = (now, [], note)
        return [], note
    items: list[dict[str, Any]] = []
    for row in payload.get("data", []) if isinstance(payload, dict) else []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        entity = _marketaux_pick_entity(row, symbol)
        sentiment: float | None = None
        if entity is not None:
            try:
                sentiment = max(
                    -1.0, min(1.0, float(entity.get("sentiment_score")))
                )
            except (TypeError, ValueError):
                sentiment = None
        items.append(
            {
                "source": "Marketaux",
                "title": title,
                "link": str(row.get("url") or ""),
                "published": _marketaux_published(
                    str(row.get("published_at") or "")
                ),
                "tags": tag_text(title),
                "sentiment": sentiment,
            }
        )
    note = "" if items else "no Marketaux news returned for this symbol"
    _MARKETAUX_CACHE[cache_key] = (now, items, note)
    return items, note


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def check_symbol(symbol: str, marketaux: bool | None = None) -> dict[str, Any]:
    """Check news for ``symbol`` (e.g. ``RELIANCE`` or ``RELIANCE.NS``).

    ``marketaux`` controls the optional Marketaux fetch: None (default)
    means "only for the 3 indices or symbols moving >= 3%" — the
    free-tier guard; True/False forces it on/off (callers with a setup
    can force it on). Marketaux items carry a numeric ``sentiment``
    (-1..+1); NSE/Yahoo items carry ``sentiment`` None (keyword
    sentiment is used for them instead).

    Returns a dict with:
        symbol       the symbol as given
        moving       True when |change vs prev close| >= 3%
        change_pct   change vs previous close (None if the quote failed)
        tags         sorted unique tags across all new items
        items        new (not previously seen) items, announcements first
        sources      per-source status notes
        quote_error  quote failure reason, or ""
    """
    yahoo_symbol = symbol if "." in symbol else f"{symbol}.NS"

    # Price move (best effort — a failed quote never blocks the news).
    change_pct: float | None = None
    quote_error = ""
    try:
        from bhanu.data import fetch_quote

        quote = fetch_quote(yahoo_symbol)
        change_pct = quote["change_pct"]
    except Exception as exc:  # noqa: BLE001 - report, don't crash
        quote_error = str(exc)

    # Announcements first (they beat headlines), then Marketaux, then
    # Yahoo headlines.
    nse_items, nse_note = fetch_nse_announcements(yahoo_symbol)
    _bse_items, bse_note = fetch_bse_announcements(yahoo_symbol)
    # Marketaux (optional, key-gated): indices + moving symbols only,
    # so the 100/day free plan is never blown (60-min cache + daily
    # cap inside the fetcher).
    display = symbol.replace(".NS", "").upper()
    if marketaux is None:
        include_mx = (
            display in MARKETAUX_INDEX_SEARCH
            or (change_pct is not None and abs(change_pct) >= MOVING_PCT)
        )
    else:
        include_mx = marketaux
    if include_mx:
        mx_items, mx_note = fetch_marketaux_news(yahoo_symbol)
    else:
        mx_items, mx_note = [], (
            "Marketaux: skipped — indices & moving symbols only "
            "(free-tier limit)"
        )
    yahoo_items, yahoo_note = fetch_yahoo_headlines(yahoo_symbol)
    # Google News RSS is the live headline source (Yahoo's India RSS
    # often serves days-old items) — free, no API key.
    gnews_items, gnews_note = fetch_google_news(yahoo_symbol)

    # Dedupe against the 30-day state file.
    state = _load_state()
    seen: dict[str, str] = state.get("seen", {})
    now_iso = datetime.now(timezone.utc).isoformat()
    new_items: list[dict[str, Any]] = []
    for item in nse_items + mx_items + gnews_items + yahoo_items:
        item_id = _item_id(item["source"], item["title"], item["link"])
        if item_id in seen:
            continue
        seen[item_id] = now_iso
        new_items.append({**item, "id": item_id})
    state["seen"] = seen
    _save_state(state)

    tags = sorted({tag for item in new_items for tag in item["tags"]})
    return {
        "symbol": symbol,
        "moving": bool(change_pct is not None and abs(change_pct) >= MOVING_PCT),
        "change_pct": change_pct,
        "tags": tags,
        "items": new_items,
        # "latest" is the most recent fetched items (announcements first),
        # regardless of the 30-day dedupe — for dashboard display, so the
        # latest headlines stay visible even after they were first seen.
        # "items" above is the new/unseen set only (alerts, not a feed).
        "latest": (nse_items + mx_items + gnews_items + yahoo_items)[:5],
        "sources": {
            "nse_announcements": nse_note or f"{len(nse_items)} item(s)",
            "bse_announcements": bse_note,
            "google_news": gnews_note or f"{len(gnews_items)} item(s)",
            "yahoo_headlines": yahoo_note or f"{len(yahoo_items)} item(s)",
            "marketaux": mx_note or f"{len(mx_items)} item(s)",
        },
        "quote_error": quote_error,
    }


if __name__ == "__main__":
    # Offline demo: tagging + dedupe logic only, no network.
    samples = [
        "Reliance Industries Q2 results: net profit rises 12%",
        "SEBI approves new listing norms for main board",
        "Crude oil falls as rupee strengthens against dollar",
        "Company announces bulk deal; promoter stake unchanged",
    ]
    for text in samples:
        print(f"{tag_text(text)!s:50} <- {text}")
    print("MOVING threshold: >= 3% from previous close")
    print("Primary source: NSE/BSE exchange announcements; "
          "headlines are leads, not facts.")
    print("Marketaux: optional, MARKETAUX_API_KEY env var, 100 req/day free")
    print("Live check example (needs network): check_symbol('RELIANCE')")
