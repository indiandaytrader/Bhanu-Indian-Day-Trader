"""Market data for NSE day trading, fetched from Yahoo Finance.

Sources (public, unofficial, delayed — never trade on this alone):
    * Yahoo Finance chart API (query1, with query2 as fallback)
    * Yahoo Finance quote API for a single latest quote

Rules followed here:
    * Polite access: 0.3 s pause between requests, one retry after 5 s
      only on 429 / 5xx, query2 tried only when query1 fails that way.
    * Responses are cached under ``data/cache/`` so a re-run does not
      hammer the API.
    * Never fabricate data: on failure the fetchers raise a clear
      ``DataError`` (or, where the caller prefers, return an empty
      DataFrame plus a reason string via :func:`fetch_daily_bars_safe`).
    * All timestamps are converted to Asia/Kolkata (IST).

Universe: the 3 indices (NIFTY, BANKNIFTY, SENSEX) plus ~30 liquid
NSE F&O stocks. Yahoo symbols use the ``.NS`` suffix for NSE listings.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

IST = "Asia/Kolkata"

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "data" / "cache"

# Polite-access settings.
REQUEST_PAUSE_S = 0.3
RETRY_AFTER_S = 5.0
#: Per-request timeout (seconds). A slow Yahoo must never block a
#: page for ~90s: 10s x 2 attempts x 2 hosts is the worst case, and
#: the dashboard loads its charts lazily, so a slow fetch only delays
#: one chart card, never the page.
FETCH_TIMEOUT_S = 10.0
USER_AGENT = "Mozilla/5.0 (bhanu-indian-day-trader; paper-trading research)"

# Cache lifetimes.
DAILY_CACHE_TTL = timedelta(hours=1)
INTRADAY_CACHE_TTL = timedelta(seconds=60)
QUOTE_CACHE_TTL = timedelta(seconds=30)


class DataError(Exception):
    """Raised when market data cannot be fetched or parsed."""


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------

#: The three indices, by Yahoo symbol.
INDICES: dict[str, str] = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "SENSEX": "^BSESN",
}

#: Liquid NSE F&O stocks (display name -> Yahoo symbol).
STOCKS: dict[str, str] = {
    "RELIANCE": "RELIANCE.NS",
    "HDFCBANK": "HDFCBANK.NS",
    "ICICIBANK": "ICICIBANK.NS",
    "SBIN": "SBIN.NS",
    "AXISBANK": "AXISBANK.NS",
    "KOTAKBANK": "KOTAKBANK.NS",
    "BAJFINANCE": "BAJFINANCE.NS",
    "BAJAJFINSV": "BAJAJFINSV.NS",
    "INFY": "INFY.NS",
    "TCS": "TCS.NS",
    "WIPRO": "WIPRO.NS",
    "HCLTECH": "HCLTECH.NS",
    "TECHM": "TECHM.NS",
    "TATAMOTORS": "TATAMOTORS.NS",
    "MARUTI": "MARUTI.NS",
    "M&M": "M&M.NS",
    "TATASTEEL": "TATASTEEL.NS",
    "HINDALCO": "HINDALCO.NS",
    "LT": "LT.NS",
    "ULTRACEMCO": "ULTRACEMCO.NS",
    "SUNPHARMA": "SUNPHARMA.NS",
    "DRREDDY": "DRREDDY.NS",
    "CIPLA": "CIPLA.NS",
    "ITC": "ITC.NS",
    "HINDUNILVR": "HINDUNILVR.NS",
    "NESTLEIND": "NESTLEIND.NS",
    "TITAN": "TITAN.NS",
    "ADANIENT": "ADANIENT.NS",
    "ADANIPORTS": "ADANIPORTS.NS",
    "BHARTIARTL": "BHARTIARTL.NS",
    "ONGC": "ONGC.NS",
    "NTPC": "NTPC.NS",
    "POWERGRID": "POWERGRID.NS",
    "COALINDIA": "COALINDIA.NS",
    "EICHERMOT": "EICHERMOT.NS",
    "HEROMOTOCO": "HEROMOTOCO.NS",
}

#: Full universe: indices + stocks (display name -> Yahoo symbol).
UNIVERSE: dict[str, str] = {**INDICES, **STOCKS}


# ---------------------------------------------------------------------------
# HTTP + cache helpers
# ---------------------------------------------------------------------------

def _cache_path(key: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in key)
    return CACHE_DIR / f"{safe}.json"


def _read_cache_payload(key: str) -> dict[str, Any] | None:
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, OSError, json.JSONDecodeError):
        return None


def _read_cache(key: str, ttl: timedelta) -> dict[str, Any] | None:
    payload = _read_cache_payload(key)
    if payload is None:
        return None
    try:
        saved_at = datetime.fromisoformat(payload["saved_at"])
        if datetime.now() - saved_at > ttl:
            return None
        return payload["data"]
    except (KeyError, ValueError):
        return None


def _read_stale_payload(key: str) -> tuple[dict[str, Any], str] | None:
    """A cached payload ignoring its TTL — ``(data, saved_at)`` or None.

    Used as a fallback when the live fetch fails, so a chart can still
    show real (slightly old) bars instead of nothing. The data is real
    market data from an earlier successful fetch, never fabricated.
    """
    payload = _read_cache_payload(key)
    if payload is None:
        return None
    try:
        return payload["data"], payload["saved_at"]
    except KeyError:
        return None


def _write_cache(key: str, data: dict[str, Any]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"saved_at": datetime.now().isoformat(), "data": data}
        with open(_cache_path(key), "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except OSError:
        pass  # Cache failure must never break a fetch.


def _get_json(url: str) -> dict[str, Any]:
    """GET a JSON URL: query1 first, query2 on 429/5xx, one retry after 5s."""
    hosts = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
    last_error: Exception | None = None
    for host in hosts:
        host_url = url.replace("query1.finance.yahoo.com", host, 1)
        for attempt in (1, 2):
            try:
                req = urllib.request.Request(
                    host_url, headers={"User-Agent": USER_AGENT}
                )
                with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                last_error = exc
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if retryable and attempt == 1:
                    time.sleep(RETRY_AFTER_S)
                    continue
                if retryable and host == hosts[0]:
                    break  # try the other host
                raise DataError(f"Yahoo HTTP {exc.code} for {host_url}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt == 1:
                    time.sleep(RETRY_AFTER_S)
                    continue
                raise DataError(f"network error fetching {host_url}: {exc}") from exc
            finally:
                time.sleep(REQUEST_PAUSE_S)
    raise DataError(f"could not fetch {url}: {last_error}")


def _chart_to_frame(payload: dict[str, Any], symbol: str) -> pd.DataFrame:
    """Turn a Yahoo chart-API payload into an IST-indexed OHLCV frame."""
    try:
        result = payload["chart"]["result"][0]
        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError) as exc:
        err = (payload.get("chart", {}) or {}).get("error") or {}
        raise DataError(
            f"no chart data for {symbol}: "
            f"{err.get('description', 'unexpected response shape')}"
        ) from exc
    index = pd.to_datetime(timestamps, unit="s", utc=True).tz_convert(IST)
    frame = pd.DataFrame(
        {
            "open": quote.get("open"),
            "high": quote.get("high"),
            "low": quote.get("low"),
            "close": quote.get("close"),
            "volume": quote.get("volume"),
        },
        index=index,
    )
    frame.index.name = "timestamp"
    return frame.dropna(subset=["close"])


# ---------------------------------------------------------------------------
# Public fetchers
# ---------------------------------------------------------------------------

def fetch_daily_bars(symbol: str, days: int = 30) -> pd.DataFrame:
    """Daily OHLCV bars for ``symbol`` (Yahoo symbol, e.g. ``RELIANCE.NS``).

    Returns a DataFrame with columns open/high/low/close/volume indexed
    by IST datetime. Raises :class:`DataError` on failure — never returns
    fabricated bars.
    """
    cache_key = f"daily_{symbol}_{days}d"
    cached = _read_cache(cache_key, DAILY_CACHE_TTL)
    if cached is not None:
        return _chart_to_frame(cached, symbol)
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?range={days}d&interval=1d"
    )
    try:
        payload = _get_json(url)
    except DataError:
        # Live fetch failed — serve the last cached bars (real data,
        # just older) instead of nothing.
        stale = _read_stale_payload(cache_key)
        if stale is not None:
            return _chart_to_frame(stale[0], symbol)
        raise
    _write_cache(cache_key, payload)
    return _chart_to_frame(payload, symbol)


def fetch_daily_bars_safe(symbol: str, days: int = 30) -> tuple[pd.DataFrame, str]:
    """Like :func:`fetch_daily_bars` but returns ``(frame, reason)``.

    On failure ``frame`` is empty and ``reason`` explains why, so a
    scanner can report partial results instead of crashing.
    """
    try:
        return fetch_daily_bars(symbol, days=days), ""
    except DataError as exc:
        return pd.DataFrame(
            columns=["open", "high", "low", "close", "volume"]
        ), str(exc)


def fetch_intraday_bars_meta(symbol: str, interval: str = "5m",
                             days: int = 1) -> tuple[pd.DataFrame, bool, str]:
    """Like :func:`fetch_intraday_bars` but also reports staleness:
    ``(frame, stale, as_of)`` — ``stale`` is True when the bars came
    from an expired disk cache because the live fetch failed, and
    ``as_of`` is that cache's save time ('' when the bars are fresh).
    The stale bars are real market data from an earlier successful
    fetch, never fabricated.
    """
    cache_key = f"intraday_{symbol}_{interval}_{days}d"
    cached = _read_cache(cache_key, INTRADAY_CACHE_TTL)
    if cached is not None:
        return _chart_to_frame(cached, symbol), False, ""
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?range={days}d&interval={interval}"
    )
    try:
        payload = _get_json(url)
    except DataError:
        stale = _read_stale_payload(cache_key)
        if stale is not None:
            data, saved_at = stale
            return _chart_to_frame(data, symbol), True, saved_at
        raise
    _write_cache(cache_key, payload)
    return _chart_to_frame(payload, symbol), False, ""


def fetch_intraday_bars(symbol: str, interval: str = "5m", days: int = 1) -> pd.DataFrame:
    """Intraday OHLCV bars for ``symbol`` at ``interval`` (default 5m).

    ``days`` sets how many sessions of bars to fetch (default 1 = today
    only; 5 = the last 5 sessions, used for swing levels). Returns a
    DataFrame with the same shape as :func:`fetch_daily_bars`, indexed
    by IST datetime. Raises :class:`DataError` on failure — unless a
    stale disk cache exists, in which case those (real, older) bars
    are returned instead of raising.
    """
    frame, _stale, _as_of = fetch_intraday_bars_meta(symbol, interval, days)
    return frame


def fetch_quote(symbol: str) -> dict[str, Any]:
    """Latest quote for ``symbol``.

    Returns ``{"symbol", "price", "prev_close", "change_pct"}`` where
    ``change_pct`` is vs the previous close. Raises :class:`DataError`
    on failure.

    Tries the v7 quote endpoint first; if that fails (it often returns
    403 for non-browser clients), falls back to the v8 chart endpoint
    (last close = price, previous close = prev close).
    """
    cache_key = f"quote_{symbol}"
    cached = _read_cache(cache_key, QUOTE_CACHE_TTL)
    if cached is not None:
        payload = cached
    else:
        url = (
            "https://query1.finance.yahoo.com/v7/finance/quote"
            f"?symbols={symbol}"
        )
        try:
            payload = _get_json(url)
        except DataError:
            payload = None
        if payload is not None:
            _write_cache(cache_key, payload)
            try:
                result = payload["quoteResponse"]["result"][0]
            except (KeyError, IndexError, TypeError):
                result = {}
            price = result.get("regularMarketPrice")
            prev_close = result.get("regularMarketPreviousClose")
            if price is not None and prev_close is not None:
                change_pct = (price - prev_close) / prev_close * 100.0
                return {
                    "symbol": symbol,
                    "price": float(price),
                    "prev_close": float(prev_close),
                    "change_pct": float(change_pct),
                }
        # Fallback: v8 chart endpoint (works where v7 quote is blocked).
        frame = fetch_daily_bars(symbol, days=5)
        if frame.empty or len(frame) < 2:
            raise DataError(f"no quote data for {symbol}")
        closes = frame["close"].dropna()
        if len(closes) < 2:
            raise DataError(f"quote for {symbol} is missing price/prev_close")
        price = float(closes.iloc[-1])
        prev_close = float(closes.iloc[-2])
        change_pct = (price - prev_close) / prev_close * 100.0
        return {
            "symbol": symbol,
            "price": price,
            "prev_close": prev_close,
            "change_pct": change_pct,
        }


def fetch_company_summary(symbol: str) -> str | None:
    """One-line company description for ``symbol`` (Yahoo assetProfile).

    Returns the ``longBusinessSummary`` string, or ``None`` when Yahoo
    does not return one. Yahoo's quoteSummary endpoint currently
    returns HTTP 401 for non-browser clients from this server, so
    ``None`` is the normal outcome here — callers must show
    "unavailable", never invent a description. Cached 1 hour (the
    summary changes rarely).
    """
    cache_key = f"summary_{symbol}"
    cached = _read_cache(cache_key, DAILY_CACHE_TTL)
    if cached is not None:
        return cached.get("summary") or None
    url = (
        "https://query1.finance.yahoo.com/v10/finance/quoteSummary/"
        f"{symbol}?modules=assetProfile"
    )
    try:
        payload = _get_json(url)
        profile = payload["quoteSummary"]["result"][0]["assetProfile"]
        summary = (profile.get("longBusinessSummary") or "").strip()
    except Exception:  # noqa: BLE001 - any failure means "unavailable"
        return None
    if not summary:
        return None
    _write_cache(cache_key, {"summary": summary})
    return summary


# ---------------------------------------------------------------------------
# Offline test helper
# ---------------------------------------------------------------------------

def make_synthetic_bars(
    n: int = 60,
    start_price: float = 1000.0,
    seed: int = 42,
    start: str = "2026-11-02 09:15",
    freq: str = "B",
) -> pd.DataFrame:
    """Deterministic synthetic OHLCV bars for offline testing.

    Random-walk closes around ``start_price``; highs/lows bracket the
    open/close of each bar. No real market data — test fixture only.
    """
    rng = np.random.default_rng(seed)
    index = pd.date_range(start=start, periods=n, freq=freq, tz=IST)
    rets = rng.normal(0.0, 0.004, size=n)
    closes = start_price * np.exp(np.cumsum(rets))
    opens = np.concatenate([[start_price], closes[:-1]])
    spread = np.abs(rng.normal(0.0, 0.002, size=n)) * closes
    highs = np.maximum(opens, closes) + spread
    lows = np.minimum(opens, closes) - spread
    volumes = rng.integers(100_000, 1_000_000, size=n)
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        },
        index=index,
    )


if __name__ == "__main__":
    # Offline demo: synthetic bars only, no network.
    bars = make_synthetic_bars(n=30)
    print(f"synthetic bars: {len(bars)} rows, {bars.index[0]} -> {bars.index[-1]}")
    print(bars.head(3).round(2).to_string())
    print(f"universe: {len(UNIVERSE)} symbols "
          f"({len(INDICES)} indices + {len(STOCKS)} stocks)")
    print("live fetch example (needs network): "
          "fetch_daily_bars('RELIANCE.NS')")
