# bhanu/poller.py
"""Background live-data engine for the Bhanu Indian Day Trader dashboard.

The dashboard never fetches market data while rendering a page. Instead,
daemon threads keep an in-memory state warm at all times, and every page
and ``/api/*`` endpoint reads from that state (falling back to the
on-demand fetchers only when the engine has nothing yet):

    * indices — every NSE index (allIndices) every 15 s while the
      market is open (5 min when closed): last, % change, prev close,
      open, PE, advances/declines.
    * spark — Yahoo's batch feed, 20 symbols per call: the 3 indices
      every 2 s (the live quote feed — a new price every couple of
      seconds), all 39 symbols every 30 s (flow/sector input).
    * intraday — 5-minute candles + close line for the 3 indices every
      60 s, so the home charts draw instantly from warm data.
    * flow / sectors — all 39 symbols' intraday % change every 5 min.
    * news — automatic backend news check (NSE announcements + Yahoo
      headlines) for the 3 indices every 5 min.
    * scans — ORB scan every 2 min, levels every 2 min, Time Machine
      (10,000 Monte Carlo replays) every 10 min.
    * daily_prev — previous trading day's close per index, from Yahoo
      daily bars, every 10 min: the middle source for the pinned
      previous close (NSE has no SENSEX row and Yahoo spark's
      ``chartPreviousClose`` goes stale).
    * snapshot — today's intraday state is written to disk every
      5 minutes while open, and reloaded on start, so a restart still
      shows the day's real data instead of nothing.

Rules:
    * Last good value wins — a failed fetch never clears the state.
    * HTTP 429 triggers a global 120 s backoff for the fast feeds.
    * Stdlib only (urllib + threading); no new dependency.
    * Threads are daemons and never block a page: the engine is
      started once from ``web.app.main()``.
"""

from __future__ import annotations

import json
import os
from bhanu import upstox, webhooks
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bhanu import charts as charts_mod
from bhanu import data as data_mod
from bhanu import options as options_mod

IST = ZoneInfo("Asia/Kolkata")
REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = REPO_ROOT / "data" / "poller_snapshot.json"

# --- Cadence (seconds): fast while the market is open, slow otherwise. ---
POLL_INDICES_FAST_S = 15
POLL_INDICES_SLOW_S = 300
POLL_SPARK_FAST_S = 2
POLL_SPARK_SLOW_S = 300
#: Full-universe spark (all 39 symbols) — flow/sector input.
POLL_SPARK_FULL_FAST_S = 30
POLL_SPARK_FULL_SLOW_S = 300
POLL_INTRADAY_FAST_S = 60
POLL_INTRADAY_SLOW_S = 300
POLL_FLOW_FAST_S = 120
POLL_FLOW_SLOW_S = 600
POLL_NEWS_FAST_S = 300
POLL_NEWS_SLOW_S = 900
POLL_SCAN_FAST_S = 120
POLL_SCAN_SLOW_S = 600
POLL_TM_FAST_S = 600
POLL_TM_SLOW_S = 1800
#: Previous-close from Yahoo daily bars (the pin's middle source):
#: the value changes once a day, so 10 min / 30 min is plenty.
POLL_DAILY_PREV_FAST_S = 600
POLL_DAILY_PREV_SLOW_S = 1800
POLL_SNAPSHOT_FAST_S = 300
POLL_SNAPSHOT_SLOW_S = 3600

#: Global backoff after an HTTP 429 (seconds).
BACKOFF_429_S = 120
#: A spark last-price counts as fresh for this long (seconds).
SPARK_FRESH_S = 120

NSE_ALL_INDICES_URL = "https://www.nseindia.com/api/allIndices"
SPARK_URL = (
    "https://query1.finance.yahoo.com/v7/finance/spark"
    "?symbols={symbols}&range=1d&interval=1m"
)
#: Yahoo v8 daily chart (one symbol per call) — the previous-close
#: source of last resort's replacement: the close of the last daily
#: bar dated before today. Spark's ``chartPreviousClose`` goes stale
#: for indices (see _pin_prevs), daily bars do not.
DAILY_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    "?range=5d&interval=1d"
)
SPARK_URL_FALLBACK = SPARK_URL.replace(
    "query1.finance.yahoo.com", "query2.finance.yahoo.com", 1)
USER_AGENT = {"User-Agent": "Mozilla/5.0"}
FETCH_TIMEOUT_S = 15

#: NSE index name -> our display name.
INDEX_NAMES: dict[str, str] = {
    "NIFTY 50": "NIFTY",
    "NIFTY BANK": "BANKNIFTY",
    "SENSEX": "SENSEX",
}
INDEX_YAHOO: dict[str, str] = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "SENSEX": "^BSESN",
}
INDEX_LABELS: dict[str, str] = {
    "NIFTY": "NIFTY 50",
    "BANKNIFTY": "BANK NIFTY",
    "SENSEX": "SENSEX",
}
NEWS_SYMBOLS: tuple[str, ...] = ("NIFTY", "BANKNIFTY", "SENSEX")


class PollerError(Exception):
    """A fetch failed; ``status`` is the HTTP status when known."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# State (guarded by _LOCK; readers copy what they need)
# ---------------------------------------------------------------------------

def _fresh_state() -> dict:
    return {
        "running": False,
        "started_at": "",
        "indices": {},
        "chains": {},
        "spark": {},
        "intraday": {},
        "flow": None,
        "sectors": None,
        "news": {},
        "orb": None,
        "levels": None,
        "timemachine": None,
        "ticks": {},
        "symbols_scanned": 0,
        "orb_hits": 0,
        "timemachine_replays": 0,
        "news_checked_at": "",
        "snapshot_at": "",
        "errors": 0,
        "slow_until": 0.0,
        # Previous trading day's close from Yahoo daily bars, per
        # index: {name: {"value", "date"}}. The pin's middle source —
        # set by the daily_prev job, never by the fetch threads.
        "daily_prev": {},
        # Pinned previous close per index: {name: {"value", "source",
        # "date"}}. NSE first, daily bars second, Yahoo spark last —
        # set by get_quotes(), never by the fetch threads.
        "prev_pinned": {},
    }


STATE: dict = _fresh_state()
_LOCK = threading.Lock()
_STOP = threading.Event()
_started = False
_threads: list[threading.Thread] = []


# ---------------------------------------------------------------------------
# Clock helpers
# ---------------------------------------------------------------------------

def _now_ist() -> datetime:
    return datetime.now(IST)


def _now_iso() -> str:
    return _now_ist().strftime("%Y-%m-%d %H:%M:%S IST")


def is_open(now: datetime | None = None) -> bool:
    """True during the NSE session (weekday, 09:15–15:30 IST)."""
    t = now or _now_ist()
    if t.weekday() >= 5:
        return False
    minutes = t.hour * 60 + t.minute
    return 555 <= minutes < 930


def _interval(fast_s: float, slow_s: float) -> float:
    return fast_s if is_open() else slow_s


# ---------------------------------------------------------------------------
# Fetchers (plain urllib, browser user-agent — no session needed)
# ---------------------------------------------------------------------------

def _fetch_json(url: str, timeout: float = FETCH_TIMEOUT_S) -> dict:
    """GET a JSON URL: first host, then the fallback host on 429/5xx."""
    last_error: Exception | None = None
    for host_url in dict.fromkeys((url, url.replace(
            "query1.finance.yahoo.com", "query2.finance.yahoo.com", 1))):
        try:
            req = urllib.request.Request(host_url, headers=USER_AGENT)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = PollerError(f"HTTP {exc.code} for {host_url}",
                                     status=exc.code)
            if exc.code == 429 or 500 <= exc.code < 600:
                continue
            raise last_error from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = PollerError(f"network error for {host_url}: {exc}")
            continue
    raise last_error


def _f(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_all_indices(payload: dict) -> dict[str, dict]:
    """NSE allIndices payload -> {display name: index fields}."""
    out: dict[str, dict] = {}
    for row in (payload or {}).get("data", []) or []:
        name = INDEX_NAMES.get(str(row.get("index", "")))
        if not name:
            continue
        last = _f(row.get("last"))
        week_ago = _f(row.get("oneWeekAgoVal"))
        out[name] = {
            "name": name,
            "last": last,
            "pct": _f(row.get("percentChange")),
            "chg": _f(row.get("variation")),
            "prev": _f(row.get("previousClose")),
            "open": _f(row.get("open")),
            "pe": _f(row.get("pe")),
            "adv": row.get("advances"),
            "dec": row.get("declines"),
            "high52": _f(row.get("yearHigh")),
            "low52": _f(row.get("yearLow")),
            "w1": round((last / week_ago - 1) * 100, 2)
            if last and week_ago else None,
            "m1": _f(row.get("perChange30d")),
            "y1": _f(row.get("perChange365d")),
        }
    return out


def fetch_nse_indices() -> dict[str, dict]:
    """All NSE indices, parsed to our display names."""
    return parse_all_indices(_fetch_json(NSE_ALL_INDICES_URL))


def parse_spark(payload: dict) -> dict[str, dict]:
    """Yahoo spark payload -> {yahoo symbol: ltp/prev/time/closes}.

    ``closes`` is a list of ``{"t": unix seconds, "c": close}`` for the
    1-minute closes of today, with missing closes dropped.
    """
    out: dict[str, dict] = {}
    for res in ((payload or {}).get("spark", {}) or {}).get("result", []) or []:
        symbol = res.get("symbol")
        response = (res.get("response") or [None])[0]
        if not symbol or not response or not response.get("timestamp"):
            continue
        meta = response.get("meta", {}) or {}
        quote = ((response.get("indicators", {}) or {}).get("quote")
                 or [{}])[0]
        closes_raw = quote.get("close") or []
        stamps = response["timestamp"]
        closes = [
            {"t": int(ts), "c": round(float(c), 2)}
            for ts, c in zip(stamps, closes_raw)
            if c is not None
        ]
        if not closes:
            continue
        out[symbol] = {
            "ltp": _f(meta.get("regularMarketPrice")),
            "prev": _f(meta.get("chartPreviousClose")),
            "time": meta.get("regularMarketTime") or 0,
            "closes": closes,
        }
    return out


def fetch_spark(symbols: list[str]) -> dict[str, dict]:
    """Batched Yahoo spark fetch, 20 symbols per call.

    Raises :class:`PollerError` (with ``status``) on failure; a 429
    must be treated as a global backoff by the caller.
    """
    out: dict[str, dict] = {}
    for i in range(0, len(symbols), 20):
        batch = symbols[i:i + 20]
        query = ",".join(
            urllib.parse.quote(s) for s in batch)
        url = SPARK_URL.format(symbols=query)
        try:
            payload = _fetch_json(url)
        except PollerError:
            # _fetch_json already tried the fallback host; surface it.
            raise
        out.update(parse_spark(payload))
        time.sleep(0.08)
    return out


def parse_daily_prev(payload: dict, today=None) -> dict | None:
    """Yahoo v8 daily chart payload -> previous trading day's close.

    Returns ``{"value", "date"}`` for the last daily bar dated
    strictly before ``today`` (IST), or None when there is no such
    bar. The bar for ``today`` itself (partial while the market is
    open) is never the previous close; before the open, the last bar
    IS yesterday's close and is picked up the same way.
    """
    result = ((payload or {}).get("chart", {}) or {}).get("result") or [None]
    row = result[0] or {}
    stamps = row.get("timestamp") or []
    quote = ((row.get("indicators", {}) or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    today = (today or _now_ist().date()).isoformat()
    best = None
    for ts, close in zip(stamps, closes):
        if ts is None or close is None:
            continue
        day = datetime.fromtimestamp(int(ts), tz=IST).date().isoformat()
        if day < today:
            best = {"value": float(close), "date": day}
    return best


def fetch_daily_prev(symbol: str) -> dict | None:
    """Previous close for one Yahoo symbol from its daily bars."""
    url = DAILY_URL.format(symbol=urllib.parse.quote(symbol))
    return parse_daily_prev(_fetch_json(url))


# ---------------------------------------------------------------------------
# Tick jobs (each keeps the last good value on failure)
# ---------------------------------------------------------------------------

def _bump_errors() -> None:
    with _LOCK:
        STATE["errors"] += 1


def _mark_tick(name: str) -> None:
    with _LOCK:
        STATE["ticks"][name] = _now_iso()


def _tick_indices() -> None:
    try:
        indices = fetch_nse_indices()
    except Exception:  # noqa: BLE001 - keep last good value
        _bump_errors()
        return
    if indices:
        with _LOCK:
            STATE["indices"] = indices
        _mark_tick("indices")


def _do_spark(symbols: list[str]) -> bool:
    """One batched spark fetch into STATE. Returns False on failure —
    a 429 also sets the global backoff, and the last good value stays."""
    if time.time() < STATE["slow_until"]:
        return False
    try:
        spark = fetch_spark(symbols)
    except PollerError as exc:
        if exc.status == 429:
            with _LOCK:
                STATE["slow_until"] = time.time() + BACKOFF_429_S
        _bump_errors()
        return False
    except Exception:  # noqa: BLE001 - keep last good value
        _bump_errors()
        return False
    if spark:
        with _LOCK:
            STATE["spark"].update(spark)
        return True
    return False


def _tick_spark_fast() -> None:
    """Live quote feed: the 3 indices every few seconds."""
    if _do_spark([INDEX_YAHOO[name]
                  for name in ("NIFTY", "BANKNIFTY", "SENSEX")]):
        _mark_tick("spark_fast")


def _tick_spark_full() -> None:
    """Full universe spark every 60 s (flow/sector input)."""
    if _do_spark(list(dict.fromkeys(data_mod.UNIVERSE.values()))):
        _mark_tick("spark_full")


def _tick_intraday() -> None:
    ok = False
    for symbol in ("^NSEI", "^NSEBANK", "^BSESN"):
        try:
            payload = charts_mod.intraday_series(symbol)
        except Exception:  # noqa: BLE001 - keep last good value
            _bump_errors()
            continue
        if payload.get("error"):
            _bump_errors()
            continue
        with _LOCK:
            STATE["intraday"][symbol] = payload
        ok = True
    if ok:
        _mark_tick("intraday")


def _tick_flow() -> None:
    try:
        flow = charts_mod.flow_series()
        sectors = charts_mod.sector_map(flow)
    except Exception:  # noqa: BLE001 - keep last good value
        _bump_errors()
        return
    with _LOCK:
        STATE["flow"] = flow
        STATE["sectors"] = sectors
        STATE["symbols_scanned"] = len(flow.get("series", [])) + len(
            flow.get("failed", []))
    _mark_tick("flow")


def _tick_news() -> None:
    from bhanu import news as news_mod

    ok = False
    from bhanu import paper as paper_mod

    try:
        positions = paper_mod.list_positions()
    except (OSError, ValueError):
        positions = []
    symbols = dict.fromkeys((*NEWS_SYMBOLS, *(p["symbol"] for p in positions)))
    for symbol in symbols:
        try:
            result = news_mod.check_symbol(symbol)
        except Exception:  # noqa: BLE001 - keep last good value
            _bump_errors()
            continue
        with _LOCK:
            STATE["news"][symbol] = result
        ok = True
    if ok:
        with _LOCK:
            STATE["news_checked_at"] = _now_iso()
        _mark_tick("news")


def _tick_scans() -> None:
    from bhanu import levels as levels_mod
    from bhanu import scanner as scanner_mod

    try:
        orb = scanner_mod.scan_orb()
        scanner_mod.write_watchlist(orb)
    except Exception:  # noqa: BLE001 - keep last good value
        _bump_errors()
    else:
        with _LOCK:
            STATE["orb"] = orb
            STATE["orb_hits"] = len(orb.get("hits", []))
    try:
        levels = levels_mod.scan_levels()
    except Exception:  # noqa: BLE001 - keep last good value
        _bump_errors()
    else:
        with _LOCK:
            STATE["levels"] = levels
    _mark_tick("scans")


def _tick_timemachine() -> None:
    from bhanu import timecheck as timecheck_mod

    try:
        result = timecheck_mod.collect_timemachine()
    except Exception:  # noqa: BLE001 - keep last good value
        _bump_errors()
        return
    with _LOCK:
        STATE["timemachine"] = result
        mc = (result or {}).get("monte_carlo", {}) or {}
        STATE["timemachine_replays"] = int(mc.get("n", 0) or 0)
    _mark_tick("timemachine")


def _snapshot_payload() -> dict:
    with _LOCK:
        spark = {
            sym: {"ltp": val.get("ltp"), "prev": val.get("prev")}
            for sym, val in STATE["spark"].items()
        }
        return {
            "date": _now_ist().date().isoformat(),
            "as_of": _now_iso(),
            "indices": dict(STATE["indices"]),
            "spark": spark,
            "intraday": dict(STATE["intraday"]),
        }


def _tick_daily_prev() -> None:
    """Previous trading day's close per index, from Yahoo daily
    bars. The pin's middle source: NSE has no SENSEX row and Yahoo
    spark's ``chartPreviousClose`` goes stale (see _pin_prevs)."""
    ok = False
    for name in ("NIFTY", "BANKNIFTY", "SENSEX"):
        try:
            prev = fetch_daily_prev(INDEX_YAHOO[name])
        except Exception:  # noqa: BLE001 - keep last good value
            _bump_errors()
            continue
        if not prev:
            _bump_errors()
            continue
        with _LOCK:
            STATE.setdefault("daily_prev", {})[name] = prev
        ok = True
    if ok:
        _mark_tick("daily_prev")


def _tick_snapshot() -> None:
    if not is_open():
        return
    try:
        payload = _snapshot_payload()
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SNAPSHOT_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(SNAPSHOT_PATH)
    except OSError:
        _bump_errors()
        return
    with _LOCK:
        STATE["snapshot_at"] = _now_iso()
    _mark_tick("snapshot")


def _load_snapshot() -> bool:
    """Reload today's snapshot (if any) so a restart still shows the
    day's real data. Returns True when a same-day snapshot was loaded."""
    try:
        payload = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if payload.get("date") != _now_ist().date().isoformat():
        return False
    with _LOCK:
        if payload.get("indices"):
            STATE["indices"] = payload["indices"]
        if payload.get("spark"):
            STATE["spark"] = {
                sym: {"ltp": v.get("ltp"), "prev": v.get("prev"),
                      "time": 0, "closes": []}
                for sym, v in payload["spark"].items()
            }
        if payload.get("intraday"):
            STATE["intraday"] = payload["intraday"]
        STATE["snapshot_at"] = payload.get("as_of", "")
    return True


# ---------------------------------------------------------------------------
# Engine control
# ---------------------------------------------------------------------------

def _tick_chains() -> None:
    for symbol in ("NIFTY", "BANKNIFTY"):
        try:
            result = options_mod.fetch_chain(symbol)
        except Exception as exc:
            result = {"error": str(exc) or "NSE feed unavailable"}
        if result.get("error") and os.environ.get("UPSTOX_ACCESS_TOKEN"):
            nse_error = result["error"]
            result = upstox.fetch(symbol)
            if result.get("error"):
                result = {"error": f"{nse_error} · {result['error']}"}
        error = result.get("error")
        with _LOCK:
            prior = STATE["chains"].get(symbol, {})
            STATE["chains"][symbol] = {
                "data": prior.get("data") if error else result,
                "received": prior.get("received") if error else _now_iso(),
                "error": error,
            }
        if error:
            _bump_errors()
        else:
            _mark_tick("chain_" + symbol.lower())


def get_chain(symbol: str) -> dict:
    """Published chain snapshots are replaced atomically and never mutated."""
    with _LOCK:
        return dict(STATE["chains"].get(symbol, {}))


def _tick_alerts():
    if not (os.environ.get("DISCORD_WEBHOOK_URL") or os.environ.get("TELEGRAM_WEBHOOK_URL")):
        return
    now = _now_ist()
    extra = ()
    if os.environ.get("TELEGRAM_WEBHOOK_URL"):
        from bhanu import config, paper
        with _LOCK:
            news = [item for result in STATE["news"].values() for item in result.get("items", [])]
        try:
            risk = paper.day_summary(now.date())["risk_taken"]
            limit = config.load_config().max_daily_loss
        except (OSError, ValueError, KeyError):
            _bump_errors()
            risk, limit = 0, None
        ticks = {symbol: get_spark(key) for symbol, key in INDEX_YAHOO.items()}
        extra = webhooks.snapshot_alerts(now, (get_levels() or {}).get("levels", []),
                                        ticks, news, risk, limit)
    if webhooks.deliver(now, (get_orb() or {}).get("hits", []), extra):
        _bump_errors()


_JOBS: tuple[tuple[str, float, float, object], ...] = (
    ("paper_alerts", 20, 20, _tick_alerts),
    ("chains", 30, 900, _tick_chains),
    ("indices", POLL_INDICES_FAST_S, POLL_INDICES_SLOW_S, _tick_indices),
    ("spark_fast", POLL_SPARK_FAST_S, POLL_SPARK_SLOW_S, _tick_spark_fast),
    ("spark_full", POLL_SPARK_FULL_FAST_S, POLL_SPARK_FULL_SLOW_S,
     _tick_spark_full),
    ("intraday", POLL_INTRADAY_FAST_S, POLL_INTRADAY_SLOW_S, _tick_intraday),
    ("flow", POLL_FLOW_FAST_S, POLL_FLOW_SLOW_S, _tick_flow),
    ("news", POLL_NEWS_FAST_S, POLL_NEWS_SLOW_S, _tick_news),
    ("scans", POLL_SCAN_FAST_S, POLL_SCAN_SLOW_S, _tick_scans),
    ("timemachine", POLL_TM_FAST_S, POLL_TM_SLOW_S, _tick_timemachine),
    ("daily_prev", POLL_DAILY_PREV_FAST_S, POLL_DAILY_PREV_SLOW_S,
     _tick_daily_prev),
    ("snapshot", POLL_SNAPSHOT_FAST_S, POLL_SNAPSHOT_SLOW_S, _tick_snapshot),
)


def _loop(name: str, fast_s: float, slow_s: float, fn) -> None:
    while not _STOP.is_set():
        try:
            fn()
        except Exception:  # noqa: BLE001 - a job must never kill its thread
            _bump_errors()
        _STOP.wait(_interval(fast_s, slow_s))


def start() -> bool:
    """Start the engine (idempotent). Returns True when running."""
    global _started
    with _LOCK:
        if _started:
            return True
        _started = True
    _STOP.clear()
    _load_snapshot()
    with _LOCK:
        STATE["running"] = True
        STATE["started_at"] = _now_iso()
    for name, fast_s, slow_s, fn in _JOBS:
        thread = threading.Thread(
            target=_loop, args=(name, fast_s, slow_s, fn),
            daemon=True, name=f"poller-{name}")
        thread.start()
        _threads.append(thread)
    return True


def stop() -> None:
    """Stop the engine (tests / shutdown)."""
    global _started
    _STOP.set()
    with _LOCK:
        _started = False
        STATE["running"] = False
    for thread in list(_threads):
        thread.join(timeout=2)
    _threads.clear()


def is_running() -> bool:
    return bool(STATE.get("running"))


# ---------------------------------------------------------------------------
# Readers — what the dashboard serves (copies, never the live dicts)
# ---------------------------------------------------------------------------

#: Previous-close source priority (higher wins, see _pin_prevs).
_PIN_PRIORITY = {"yahoo": 1, "daily": 2, "nse": 3}


def _pin_prevs(indices: dict, spark: dict) -> dict:
    """Pin each index's previous close to ONE source per trading day
    (lock must be held). Source priority: NSE's ``previousClose``
    first, then the previous trading day's close from Yahoo daily
    bars, then Yahoo spark's ``chartPreviousClose`` as a last resort.

    A pin only ever upgrades to a strictly higher-priority source;
    it never downgrades and never flips. An NSE pin is never
    replaced. The daily-bars source exists because Yahoo spark's
    ``chartPreviousClose`` goes stale for indices: on 26 Nov 2026 it
    still said SENSEX 82,436.00 (the 23 Nov close) while the true
    25 Nov close was 83,938.28 — and NSE lists no SENSEX row, so
    SENSEX had only the stale Yahoo fallback and showed +2.30%
    instead of +0.47%. Yahoo's spark prev is stale for NIFTY
    (25,349.70) and BANKNIFTY (62,585.40) too; their correct NSE pins
    only masked it. If NSE ever drops, the daily-bars source now
    catches all three instead of the stale spark value."""
    pinned = STATE.setdefault("prev_pinned", {})
    daily = STATE.get("daily_prev", {})
    today = _now_ist().date().isoformat()
    for name in ("NIFTY", "BANKNIFTY", "SENSEX"):
        yahoo = INDEX_YAHOO[name]
        candidates = []
        nse_prev = indices.get(name, {}).get("prev")
        if nse_prev is not None:
            candidates.append((3, float(nse_prev), "nse"))
        daily_prev = daily.get(name, {}).get("value")
        if daily_prev is not None:
            candidates.append((2, float(daily_prev), "daily"))
        yahoo_prev = spark.get(yahoo, {}).get("prev")
        if yahoo_prev is not None:
            candidates.append((1, float(yahoo_prev), "yahoo"))
        if not candidates:
            continue
        best_prio, best_val, best_src = max(
            candidates, key=lambda c: c[0])
        pin = pinned.get(name)
        if pin is None or pin.get("date") != today:
            pinned[name] = {"value": best_val, "source": best_src,
                            "date": today}
        elif _PIN_PRIORITY.get(pin.get("source"), 0) < best_prio:
            # One-way upgrade to a better source (Yahoo -> daily,
            # Yahoo/daily -> NSE). Never the reverse direction.
            pinned[name] = {"value": best_val, "source": best_src,
                            "date": today}
    return dict(pinned)


def get_prev_close(yahoo_symbol: str) -> float | None:
    """Pinned previous close for one index (same rule as
    :func:`get_quotes`), so every endpoint reports the same base."""
    name = next((k for k, v in INDEX_YAHOO.items()
                 if v == yahoo_symbol), None)
    if name is None:
        return None
    with _LOCK:
        pinned = _pin_prevs(dict(STATE["indices"]), dict(STATE["spark"]))
    pin = pinned.get(name)
    return pin["value"] if pin else None


def get_quotes() -> list[dict] | None:
    """Live index quotes: NSE first, fresh Yahoo spark overrides the
    last price. Returns None when the engine has nothing yet.

    The previous close is pinned to ONE source per trading day — see
    :func:`_pin_prevs`. The price itself still takes the fresh spark
    ltp (5 s ticks); % change is always recomputed from that price
    and the pinned previous close, so the three numbers always agree."""
    with _LOCK:
        indices = dict(STATE["indices"])
        spark = dict(STATE["spark"])
        pinned = _pin_prevs(indices, spark)
    if not indices and not spark:
        return None
    out: list[dict] = []
    now = time.time()
    for name in ("NIFTY", "BANKNIFTY", "SENSEX"):
        yahoo = INDEX_YAHOO[name]
        ix = indices.get(name, {})
        price = ix.get("last")
        pin = pinned.get(name)
        prev = pin["value"] if pin else None
        sp = spark.get(yahoo, {})
        ltp = sp.get("ltp")
        fresh = (ltp is not None
                 and (now - (sp.get("time") or 0)) < SPARK_FRESH_S)
        if fresh or price is None:
            # Fresh spark ltp is the live price; when NSE has no
            # quote at all (SENSEX) the last spark value still beats
            # an empty card.
            if ltp is not None:
                price = ltp
        if price is not None and prev:
            pct = round((price / prev - 1) * 100, 2)
        else:
            pct = None
        if price is None:
            out.append({"symbol": yahoo, "label": INDEX_LABELS[name],
                        "price": None, "prev_close": prev,
                        "change_pct": pct, "error": "no live quote yet"})
        else:
            out.append({"symbol": yahoo, "label": INDEX_LABELS[name],
                        "price": float(price),
                        "prev_close": float(prev) if prev is not None else None,
                        "change_pct": pct, "error": None})
    return out


def get_spark(yahoo_symbol: str) -> dict | None:
    """Warmed spark entry for one symbol (ltp/prev/time/1m closes),
    or None when the engine has not had a spark tick yet."""
    with _LOCK:
        entry = STATE["spark"].get(yahoo_symbol)
    return dict(entry) if entry else None


def get_intraday(yahoo_symbol: str) -> dict | None:
    """Warmed intraday payload for one index (or None when not yet)."""
    with _LOCK:
        payload = STATE["intraday"].get(yahoo_symbol)
    return dict(payload) if payload else None


def get_flow() -> dict | None:
    """Warmed Smart Money Flow payload (or None when not yet)."""
    with _LOCK:
        flow = STATE["flow"]
    return dict(flow) if flow else None


def get_sectors() -> dict | None:
    """Warmed sector map payload (or None when not yet)."""
    with _LOCK:
        sectors = STATE["sectors"]
    return dict(sectors) if sectors else None


def get_news(symbol: str) -> dict | None:
    """Warmed news check for one index (or None when not yet)."""
    with _LOCK:
        result = STATE["news"].get(symbol)
    return dict(result) if result else None


def get_orb() -> dict | None:
    """Warmed ORB scan result (or None when not yet)."""
    with _LOCK:
        orb = STATE["orb"]
    return dict(orb) if orb else None


def get_levels() -> dict | None:
    """Warmed levels scan result (or None when not yet)."""
    with _LOCK:
        levels = STATE["levels"]
    return dict(levels) if levels else None


def get_timemachine() -> dict | None:
    """Warmed Time Machine result (or None when not yet)."""
    with _LOCK:
        result = STATE["timemachine"]
    return dict(result) if result else None


def status() -> dict:
    """Engine status for the robot card and /api/poller (in-memory,
    instant — this never fetches)."""
    with _LOCK:
        ticks = dict(STATE["ticks"])
        snapshot = {
            "running": bool(STATE["running"]),
            "started_at": STATE["started_at"],
            "market_open": is_open(),
            "ticks": ticks,
            "symbols_scanned": STATE["symbols_scanned"],
            "spark_symbols": len(STATE["spark"]),
            "orb_hits": STATE["orb_hits"],
            "news_checked_at": STATE["news_checked_at"],
            "timemachine_replays": STATE["timemachine_replays"],
            "snapshot_at": STATE["snapshot_at"],
            "errors": STATE["errors"],
            "backoff_until": (
                datetime.fromtimestamp(STATE["slow_until"], tz=IST)
                .strftime("%Y-%m-%d %H:%M:%S IST")
                if STATE["slow_until"] > time.time() else ""
            ),
        }
    fresh = False
    try:
        for stamp in ticks.values():
            when = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S IST")
            when = when.replace(tzinfo=IST)
            if (_now_ist() - when).total_seconds() < 120:
                fresh = True
                break
    except (ValueError, TypeError):
        fresh = False
    snapshot["fresh"] = fresh
    return snapshot


if __name__ == "__main__":
    print("live engine: indices every "
          f"{POLL_INDICES_FAST_S}s open / {POLL_INDICES_SLOW_S}s closed, "
          f"spark batched 20/call, snapshot every {POLL_SNAPSHOT_FAST_S}s")
    print(f"universe: {len(data_mod.UNIVERSE)} symbols "
          f"({len(data_mod.INDICES)} indices)")
