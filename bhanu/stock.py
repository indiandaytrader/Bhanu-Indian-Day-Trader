"""Per-stock detail data for the /stock page (detail card with setup,
stats and chart).

Original code. Everything here is computed from the symbol's own real
bars (Yahoo 5m, last 60 days) or reported as unavailable — nothing is
invented:

* ``resolve_symbol`` — a display name (``RELIANCE``), a Yahoo symbol
  (``RELIANCE.NS``, ``^NSEI``) or an unknown name (error).
* ``sector_for`` — the symbol's sector from ``bhanu.charts.SECTORS``
  (our universe's NSE groupings), or ``None`` when the symbol is not
  in any group. Indices report ``"Index"``.
* ``rank_in_flow`` — the symbol's rank by today's % change inside the
  39-symbol universe (``OF 39`` — never any other total).
* ``orb_history`` — the ORB rule (09:15-09:30 range, close-confirmation
  trigger, range-midpoint stop, 2R target, flat by 15:15) re-run on
  every past full session of the symbol's own 5m bars: fires, wins,
  losses, neither, avg R, win rate, net R — plus the per-session
  trade list (for the chart's past-signal markers). The outcome walk
  mirrors ``bhanu.timemachine.history_replay`` exactly (stop checked
  before target; a session that never reaches the entry is not
  counted). Fewer than 20 full sessions means "not enough data".
* ``simple_words`` — one plain line about the company: a hardcoded
  line for the three indices, else Yahoo's ``longBusinessSummary``
  when it is available, else ``None`` (the page then says so).
"""

from __future__ import annotations

import importlib.util
import time
from datetime import date
from pathlib import Path
from typing import Any

from bhanu import charts as charts_mod
from bhanu import data as data_mod
from bhanu import timemachine as tm_mod

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Minimum full sessions before any backtest verdict is shown.
MIN_SESSIONS = tm_mod.MIN_SESSIONS
#: How long a computed ORB history stays cached (seconds).
HIST_CACHE_TTL = 600.0

#: Plain one-liners for the three indices (public facts, not data).
INDEX_LINES: dict[str, str] = {
    "NIFTY": "NSE ke 50 sabse bade stocks ka index — poori market ka mood batata hai.",
    "BANKNIFTY": "NSE ke 12 sabse bade banks ka index.",
    "SENSEX": "BSE ke 30 sabse bade stocks ka index.",
}

_HIST_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


# ---------------------------------------------------------------------------
# Symbol resolution / sector / rank
# ---------------------------------------------------------------------------

def resolve_symbol(query: str | None) -> tuple[str, str, bool]:
    """Resolve a user-supplied symbol to ``(label, yahoo_symbol, in_universe)``.

    ``query`` may be a display name (``RELIANCE``), a Yahoo symbol
    (``RELIANCE.NS``, ``^NSEI``) or anything else (``ValueError``).
    A Yahoo symbol outside our universe still resolves (``in_universe``
    False) — its sector and rank are then unavailable, never invented.
    """
    q = (query or "").strip().upper()
    if not q:
        raise ValueError("no symbol given")
    if q in data_mod.UNIVERSE:
        return q, data_mod.UNIVERSE[q], True
    for name, ys in data_mod.UNIVERSE.items():
        if ys.upper() == q:
            return name, ys, True
    if q.endswith(".NS"):
        return q[:-3], q, False
    if q.startswith("^"):
        return q, q, False
    raise ValueError(f"unknown symbol {query!r}")


def sector_for(label: str) -> str | None:
    """Sector for ``label`` from our universe's NSE groupings.

    Returns ``"Index"`` for the three indices, the sector name for a
    stock in a group, and ``None`` when the symbol is in no group —
    callers show "unavailable", never a guessed sector.
    """
    if label in data_mod.INDICES:
        return "Index"
    for name, members in charts_mod.SECTORS.items():
        if label in members:
            return name
    return None


def rank_in_flow(label: str, flow: dict | None) -> dict[str, Any] | None:
    """Rank of ``label`` by today's % change inside the flow ``series``.

    Returns ``{"rank", "total", "up"}`` (rank 1 = strongest) or ``None``
    when the symbol has no flow data. ``total`` is the number of
    symbols actually ranked — 39 when the whole universe reported.
    """
    series = (flow or {}).get("series") or []
    ranked = sorted(
        [s for s in series if s.get("change_pct") is not None],
        key=lambda s: s["change_pct"],
        reverse=True,
    )
    for i, s in enumerate(ranked):
        if s.get("symbol") == label:
            return {"rank": i + 1, "total": len(ranked), "up": bool(s.get("up"))}
    return None


def simple_words(label: str, yahoo_symbol: str) -> tuple[str | None, str | None]:
    """One plain line about ``label``: ``(text, source)``.

    Indices get a hardcoded plain line. Stocks get Yahoo's
    ``longBusinessSummary`` when available; otherwise ``(None, None)``
    — the page says "unavailable", it never invents a description.
    """
    if label in INDEX_LINES:
        return INDEX_LINES[label], "index"
    summary = data_mod.fetch_company_summary(yahoo_symbol)
    if summary:
        return summary, "Yahoo company summary"
    return None, None


# ---------------------------------------------------------------------------
# ORB history (per-symbol backtest + past trades)
# ---------------------------------------------------------------------------

def _load_backtest_script():
    """Import scripts/backtest_orb_nifty.py as a module (it is a script)."""
    path = REPO_ROOT / "scripts" / "backtest_orb_nifty.py"
    spec = importlib.util.spec_from_file_location("orb_backtest_stock", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sessions_from_bars(bars: list[dict]) -> list[list[dict]]:
    """Group Bar dicts into per-day sessions, keeping sessions that
    cover the whole tradable window.

    A session counts when it starts at 09:15 (the ORB range is intact)
    and reaches the flat-by bar (index 71) — it does NOT need exactly
    ``BARS_PER_SESSION`` bars: Yahoo drops a trailing bar or two for
    stocks (RELIANCE sessions run 72–74 bars, NIFTY 75), and those
    missing bars are all after the flat-by bar, so the replay is
    unaffected. A session shorter than the flat-by bar is dropped."""
    by_day: dict[date, list[dict]] = {}
    for bar in bars:
        by_day.setdefault(bar["time"].date(), []).append(bar)
    sessions: list[list[dict]] = []
    for day in sorted(by_day):
        session = sorted(by_day[day], key=lambda b: b["time"])
        if len(session) >= tm_mod.FLAT_BY_INDEX + 1:
            sessions.append(session)
    return sessions


def _touches_stop(bar: dict, stop: float, direction: str) -> bool:
    return bar["low"] <= stop if direction == "long" else bar["high"] >= stop


def _touches_target(bar: dict, target: float, direction: str) -> bool:
    return bar["high"] >= target if direction == "long" else bar["low"] <= target


def _session_outcome(session: list[dict], levels: dict, direction: str) -> float | None:
    """Outcome in R for one session's ORB trade (None = never entered).

    Mirrors ``bhanu.timemachine.history_replay``: the entry bar is the
    first bar whose close crosses the entry; stop is checked before
    target on every bar up to the flat-by bar (index 71, 15:15 IST);
    a trade still open at flat-by is scored on its last close.
    """
    entry = float(levels["entry"])
    stop = float(levels["stop"])
    target = float(levels["target"])
    risk = abs(entry - stop)
    reward = abs(target - entry)
    entry_idx = None
    for idx, bar in enumerate(session):
        crossed = bar["close"] >= entry if direction == "long" else bar["close"] <= entry
        if crossed:
            entry_idx = idx
            break
    if entry_idx is None:
        return None
    last_close = float(session[entry_idx]["close"])
    for bar in session[entry_idx : tm_mod.FLAT_BY_INDEX + 1]:
        if _touches_stop(bar, stop, direction):
            return -1.0
        if _touches_target(bar, target, direction):
            return reward / risk
        last_close = float(bar["close"])
    move = last_close - entry if direction == "long" else entry - last_close
    return move / risk


def orb_history(yahoo_symbol: str, range_: str = "60d") -> dict[str, Any]:
    """ORB backtest for ``yahoo_symbol`` on its own 5m bars.

    Returns ``{"available", "sessions", "first_day", "last_day",
    "long": {...}, "short": {...}, "trades": [...]}`` where each
    direction holds ``fires / wins / losses / neither / avg_r /
    win_rate_pct / net_r`` and ``trades`` is the per-session list
    (``date, direction, entry, stop, target, outcome_r, result``)
    for the chart's past-signal markers. ``available`` is False when
    fewer than 20 full sessions exist — the page then shows
    "not enough data", never a verdict from a tiny sample.
    Results are cached 10 minutes (the fetch + replay is slow).
    """
    now = time.time()
    cached = _HIST_CACHE.get(yahoo_symbol)
    if cached is not None and now - cached[0] < HIST_CACHE_TTL:
        return cached[1]

    def _compute() -> dict[str, Any]:
        try:
            mod = _load_backtest_script()
            bars = mod.fetch_bars_5m(yahoo_symbol, range_)
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            return {"available": False, "reason": f"could not fetch 5m bars: {exc}"}
        sessions = _sessions_from_bars(bars)
        if len(sessions) < MIN_SESSIONS:
            return {
                "available": False,
                "reason": (
                    f"not enough data — only {len(sessions)} full sessions "
                    f"(need {MIN_SESSIONS})"
                ),
                "sessions": len(sessions),
            }
        days = sorted({b["time"].date() for b in bars})
        out: dict[str, Any] = {
            "available": True,
            "sessions": len(sessions),
            "first_day": str(days[0]) if days else "?",
            "last_day": str(days[-1]) if days else "?",
            "trades": [],
        }
        for direction in ("long", "short"):
            rule = mod.orb_rule(direction)
            fires = wins = losses = neither = 0
            total_r = 0.0
            for session in sessions:
                levels = rule(session)
                if levels is None:
                    continue
                outcome = _session_outcome(session, levels, direction)
                if outcome is None:
                    continue
                fires += 1
                total_r += outcome
                if outcome > 0:
                    wins += 1
                    result = "win"
                elif outcome < 0:
                    losses += 1
                    result = "loss"
                else:
                    neither += 1
                    result = "neither"
                out["trades"].append(
                    {
                        "date": str(session[0]["time"].date()),
                        "direction": direction,
                        "entry": float(levels["entry"]),
                        "stop": float(levels["stop"]),
                        "target": float(levels["target"]),
                        "outcome_r": round(float(outcome), 3),
                        "result": result,
                    }
                )
            decisive = wins + losses
            out[direction] = {
                "fires": fires,
                "wins": wins,
                "losses": losses,
                "neither": neither,
                "avg_r": round(total_r / fires, 3) if fires else 0.0,
                "win_rate_pct": round(100.0 * wins / decisive, 1) if decisive else None,
                "net_r": round(total_r, 2),
            }
        out["trades"].sort(key=lambda t: (t["date"], t["direction"]))
        return out

    result = _compute()
    _HIST_CACHE[yahoo_symbol] = (now, result)
    return result


def main() -> int:
    print("resolve RELIANCE:", resolve_symbol("RELIANCE"))
    print("resolve HAL.NS:", resolve_symbol("HAL.NS"))
    print("sector RELIANCE:", sector_for("RELIANCE"))
    print("sector HAL:", sector_for("HAL"))
    print("index line:", simple_words("NIFTY", "^NSEI"))
    hist = orb_history("^NSEI")
    if hist.get("available"):
        for direction in ("long", "short"):
            d = hist[direction]
            print(
                f"ORB {direction}: fires={d['fires']} wins={d['wins']} "
                f"losses={d['losses']} neither={d['neither']} "
                f"avg R={d['avg_r']:+.3f} net R={d['net_r']:+.2f}"
            )
        print(f"trades listed: {len(hist['trades'])}")
    else:
        print(f"history unavailable: {hist.get('reason')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
