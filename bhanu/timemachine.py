"""Monte Carlo "time machine" + history replay for NSE day trades.

Original code, written from the rules in the DayTrader study note
(``~/workspace/goals/trading-knowledge-research/files/DAY_TRADER_SKILL_STUDY.md``).
No code is copied from the DayTrader repo or from Market Lab.

What it does
------------
Two reality checks, run *before* a trade is taken:

1. **Monte Carlo replays** (``monte_carlo``): the rest of today's session is
   replayed ``n`` times (default 10,000). The pool is the instrument's own
   last 60 days of 5-minute bars, full sessions only. Each bar is stored as
   OHLC *ratios to the previous close*; each replay draws a bar from the same
   time of day +/- 2 bars and scales it to the current price. A bar that
   touches BOTH the stop and the target counts as a stop (stop is checked
   first). Replays run to the flat-by time, 15:15 IST. The result is the share
   of replays that hit the target first, the stop first, or neither, plus the
   average outcome in R multiples.

2. **History replay** (``history_replay``): the same rule is re-run on every
   past full session, using the real bars, and the real outcomes are counted.

A Monte Carlo replay is a reality check, not proof of an edge: resampled
bars destroy autocorrelation, volatility clustering and trend persistence,
and the method assumes the next 60 days behave like the last 60. It also
ignores fees and slippage, and a gap through the stop is scored as exactly
-1R even though a real fill can be worse. The history replay (real outcomes
on real sessions) is the more honest half. Never trade on either alone.

IMPORTANT — options
-------------------
For an options trade, run this on the **option premium series** (pass 5-minute
bars of the option's own premium), never on spot/underlying bars. Theta decay
makes spot-bar replays meaningless for premium outcomes: a premium can fall
towards the stop even while spot goes nowhere. The CLI help repeats this.

NSE session
-----------
Market 09:15-15:30 IST. A full session is 75 five-minute bars
(375 minutes / 5). Flat by 15:15 IST, so the last replayed bar is the one
closing at 15:15 (bar index 71, counting 09:15-09:20 as index 0).
"""

from __future__ import annotations

import argparse
import csv
import math
import random
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Iterable, Optional, Sequence

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9 fallback, unused on 3.10+
    ZoneInfo = None  # type: ignore[assignment]

IST = ZoneInfo("Asia/Kolkata") if ZoneInfo is not None else timezone(timedelta(hours=5, minutes=30))

#: Bars in one full NSE session (09:15-15:30 IST, 5-minute bars).
BARS_PER_SESSION = 75
#: Bar index (0-based) of the bar closing at 15:15 IST — the flat-by bar.
FLAT_BY_INDEX = 71
#: Bar index just after the 09:15-09:30 opening range (first tradable bar).
FIRST_TRADE_INDEX = 3
#: Minimum full sessions required before any verdict is given.
MIN_SESSIONS = 20
#: How many days of history the replay pool is drawn from.
POOL_DAYS = 60
#: Default Monte Carlo replays and seed (seed 19 = reproducible results).
DEFAULT_N = 10_000
DEFAULT_SEED = 19

# A bar is a dict with keys: "time" (datetime), "open", "high", "low", "close".
Bar = dict

__all__ = [
    "IST",
    "BARS_PER_SESSION",
    "FLAT_BY_INDEX",
    "MIN_SESSIONS",
    "monte_carlo",
    "history_replay",
    "verdict",
    "synthetic_bars",
    "bars_from_csv",
    "Rule",
]

#: A rule re-run on one past session. It receives that session's bars and
#: returns ``{"entry": float, "stop": float, "target": float}`` for the trade
#: the rule would have taken that day, or ``None`` if the rule did not fire.
Rule = Callable[[Sequence[Bar]], Optional[dict]]


# ---------------------------------------------------------------------------
# Bar handling
# ---------------------------------------------------------------------------

def _as_datetime(value) -> datetime:
    """Coerce a timestamp value to an aware IST datetime."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time(9, 15))
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def _normalise_bars(bars_5m: Iterable[Bar]) -> list[Bar]:
    """Return bars sorted by time, as dicts with float OHLC and IST datetimes."""
    out: list[Bar] = []
    for bar in bars_5m:
        out.append(
            {
                "time": _as_datetime(bar["time"]),
                "open": float(bar["open"]),
                "high": float(bar["high"]),
                "low": float(bar["low"]),
                "close": float(bar["close"]),
            }
        )
    out.sort(key=lambda b: b["time"])
    return out


def _sessions(bars: Sequence[Bar]) -> list[list[Bar]]:
    """Group bars into per-day sessions, keeping full 75-bar sessions only."""
    by_day: dict[date, list[Bar]] = defaultdict(list)
    for bar in bars:
        by_day[bar["time"].date()].append(bar)
    sessions: list[list[Bar]] = []
    for day in sorted(by_day):
        session = sorted(by_day[day], key=lambda b: b["time"])
        if len(session) == BARS_PER_SESSION:
            sessions.append(session)
    return sessions


def _bar_index(bar_time: datetime) -> int:
    """0-based 5-minute bar index inside the session (09:15-09:20 == 0)."""
    minutes = (bar_time.hour - 9) * 60 + (bar_time.minute - 15)
    return minutes // 5


def _ratios(session: Sequence[Bar], prev_close: Optional[float]) -> list[tuple[float, float, float, float]]:
    """Each bar as OHLC ratios to the previous bar's close.

    The first bar of a session is ratioed to the previous session's last
    close when known, else to its own open (ratios of 1.0 — no move).
    """
    ratios: list[tuple[float, float, float, float]] = []
    ref = prev_close
    for bar in session:
        if ref is None or ref <= 0:
            ref = bar["open"]
        ratios.append(
            (
                bar["open"] / ref,
                bar["high"] / ref,
                bar["low"] / ref,
                bar["close"] / ref,
            )
        )
        ref = bar["close"]
    return ratios


def _check_levels(entry: float, stop: float, target: float, direction: str) -> tuple[float, float]:
    """Validate a trade setup. Returns (risk, reward) in price units."""
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")
    if entry <= 0 or stop <= 0 or target <= 0:
        raise ValueError("entry, stop and target must all be positive prices")
    if direction == "long":
        if not stop < entry < target:
            raise ValueError("for a long: stop < entry < target is required")
    else:
        if not target < entry < stop:
            raise ValueError("for a short: target < entry < stop is required")
    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk <= 0:
        raise ValueError("stop is too close to entry (zero risk)")
    return risk, reward


def _touches_stop(price_low: float, price_high: float, stop: float, direction: str) -> bool:
    return price_low <= stop if direction == "long" else price_high >= stop


def _touches_target(price_low: float, price_high: float, target: float, direction: str) -> bool:
    return price_high >= target if direction == "long" else price_low <= target


def _signed_r(final_price: float, entry: float, risk: float, direction: str) -> float:
    """Outcome of a trade in R multiples (positive = profit)."""
    move = final_price - entry if direction == "long" else entry - final_price
    return move / risk


# ---------------------------------------------------------------------------
# Monte Carlo
# ---------------------------------------------------------------------------

def monte_carlo(
    bars_5m: Iterable[Bar],
    entry: float,
    stop: float,
    target: float,
    direction: str = "long",
    n: int = DEFAULT_N,
    seed: int = DEFAULT_SEED,
    start_index: int = FIRST_TRADE_INDEX,
) -> dict:
    """Replay the rest of the session ``n`` times from ``entry``.

    Parameters
    ----------
    bars_5m:
        5-minute bars of the instrument being traded. For an OPTIONS trade
        this must be bars of the option premium itself, never spot bars.
    entry, stop, target:
        Planned entry, stop and target prices (same units as the bars).
    direction:
        ``"long"`` or ``"short"``.
    n:
        Number of replays (default 10,000).
    seed:
        Random seed (default 19) so results are reproducible.
    start_index:
        Bar index inside the session where the trade starts (default 3,
        i.e. just after the 09:15-09:30 opening range). Replays run from
        there to the flat-by bar (index 71, closing 15:15 IST).

    Returns
    -------
    dict with ``target_first_pct``, ``stop_first_pct``, ``neither_pct``,
    ``avg_r``, ``n``, ``decisive`` (target-first + stop-first count) and
    ``reward_risk`` (target distance / stop distance, i.e. R of the target).

    Raises
    ------
    ValueError if fewer than 20 full sessions are available, or if the
    entry/stop/target levels are invalid for the direction.
    """
    risk, reward = _check_levels(entry, stop, target, direction)
    if n <= 0:
        raise ValueError("n must be positive")
    if not 0 <= start_index <= FLAT_BY_INDEX:
        raise ValueError(f"start_index must be between 0 and {FLAT_BY_INDEX}")

    bars = _normalise_bars(bars_5m)
    sessions = _sessions(bars)
    if len(sessions) < MIN_SESSIONS:
        raise ValueError(
            f"need at least {MIN_SESSIONS} full sessions for a Monte Carlo "
            f"replay, got {len(sessions)}"
        )
    # Pool: the instrument's own last 60 days of full sessions.
    sessions = sessions[-POOL_DAYS:]

    # Ratios per session, and a time-of-day pool: index -> list of ratios.
    pool: dict[int, list[tuple[float, float, float, float]]] = defaultdict(list)
    prev_close: Optional[float] = None
    for session in sessions:
        ratios = _ratios(session, prev_close)
        for idx, ratio in enumerate(ratios):
            pool[idx].append(ratio)
        prev_close = session[-1]["close"]

    rng = random.Random(seed)
    target_first = stop_first = neither = 0
    total_r = 0.0

    for _ in range(n):
        price = entry
        outcome: Optional[float] = None  # R multiple once decided
        for idx in range(start_index, FLAT_BY_INDEX + 1):
            # Draw a bar from the same time of day, +/- 2 bars.
            candidates: list[tuple[float, float, float, float]] = []
            for j in (idx - 2, idx - 1, idx, idx + 1, idx + 2):
                candidates.extend(pool.get(j, []))
            if not candidates:
                break
            o_r, h_r, l_r, c_r = rng.choice(candidates)
            bar_low = price * l_r
            bar_high = price * h_r
            # Stop is checked BEFORE target: a bar touching both is a stop.
            if _touches_stop(bar_low, bar_high, stop, direction):
                outcome = -1.0
                break
            if _touches_target(bar_low, bar_high, target, direction):
                outcome = reward / risk
                break
            price = price * c_r
        if outcome is None:
            neither += 1
            outcome = _signed_r(price, entry, risk, direction)
        elif outcome > 0:
            target_first += 1
        else:
            stop_first += 1
        total_r += outcome

    return {
        "target_first_pct": 100.0 * target_first / n,
        "stop_first_pct": 100.0 * stop_first / n,
        "neither_pct": 100.0 * neither / n,
        "avg_r": total_r / n,
        "n": n,
        "decisive": target_first + stop_first,
        "reward_risk": reward / risk,
    }


# ---------------------------------------------------------------------------
# History replay
# ---------------------------------------------------------------------------

def _default_rule(entry: float, stop: float, target: float, direction: str) -> Rule:
    """Fire at the first bar whose CLOSE crosses ``entry`` in the direction."""

    def rule(session: Sequence[Bar]) -> Optional[dict]:
        for bar in session:
            crossed = bar["close"] >= entry if direction == "long" else bar["close"] <= entry
            if crossed:
                return {"entry": entry, "stop": stop, "target": target}
        return None

    return rule


def history_replay(
    bars_5m: Iterable[Bar],
    rule: Optional[Rule],
    entry: float,
    stop: float,
    target: float,
    direction: str = "long",
) -> dict:
    """Re-run the same rule on every past full session; count real outcomes.

    Parameters
    ----------
    bars_5m:
        5-minute bars. For an OPTIONS trade these must be premium bars.
    rule:
        Callable taking one session's bars and returning
        ``{"entry", "stop", "target"}`` for that session, or ``None`` if the
        rule did not fire. If ``None``, a default rule is used: fire at the
        first bar whose close crosses the given ``entry`` price, with the
        given ``stop``/``target``.
    entry, stop, target:
        Planned levels (used by the default rule; a custom rule may return
        its own per-session levels instead).
    direction:
        ``"long"`` or ``"short"``.

    Returns
    -------
    dict with ``fires``, ``target_wins``, ``stop_losses``, ``neither``,
    ``avg_r`` and ``sessions`` (full sessions examined).

    Raises
    ------
    ValueError if fewer than 20 full sessions are available.
    """
    _check_levels(entry, stop, target, direction)
    bars = _normalise_bars(bars_5m)
    sessions = _sessions(bars)
    if len(sessions) < MIN_SESSIONS:
        raise ValueError(
            f"need at least {MIN_SESSIONS} full sessions for a history replay, "
            f"got {len(sessions)}"
        )

    active_rule = rule if rule is not None else _default_rule(entry, stop, target, direction)
    fires = target_wins = stop_losses = neither = 0
    total_r = 0.0

    for session in sessions:
        levels = active_rule(session)
        if levels is None:
            continue
        lvl_entry = float(levels["entry"])
        lvl_stop = float(levels["stop"])
        lvl_target = float(levels["target"])
        try:
            risk, reward = _check_levels(lvl_entry, lvl_stop, lvl_target, direction)
        except ValueError:
            continue
        # Find the entry bar: first bar whose close crosses the entry level.
        entry_idx: Optional[int] = None
        for idx, bar in enumerate(session):
            crossed = bar["close"] >= lvl_entry if direction == "long" else bar["close"] <= lvl_entry
            if crossed:
                entry_idx = idx
                break
        if entry_idx is None:
            continue
        fires += 1
        outcome: Optional[float] = None
        last_close = session[entry_idx]["close"]
        for bar in session[entry_idx : FLAT_BY_INDEX + 1]:
            # Stop checked before target, same as the Monte Carlo.
            if _touches_stop(bar["low"], bar["high"], lvl_stop, direction):
                outcome = -1.0
                break
            if _touches_target(bar["low"], bar["high"], lvl_target, direction):
                outcome = reward / risk
                break
            last_close = bar["close"]
        if outcome is None:
            neither += 1
            outcome = _signed_r(last_close, lvl_entry, risk, direction)
        elif outcome > 0:
            target_wins += 1
        else:
            stop_losses += 1
        total_r += outcome

    return {
        "fires": fires,
        "target_wins": target_wins,
        "stop_losses": stop_losses,
        "neither": neither,
        "avg_r": (total_r / fires) if fires else 0.0,
        "sessions": len(sessions),
    }


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

def verdict(target_first_pct: float, stop_first_pct: float, reward_risk: float = 2.0) -> dict:
    """Compare the target-first share among DECISIVE replays to break-even.

    Break-even share = ``1 / (1 + R)`` where R is the target's reward/risk.
    A 2R target breaks even if it is hit first in 1/3 of decisive replays.

    Returns a dict with ``decisive_target_pct`` (target-first share among
    decisive replays only), ``break_even_pct``, ``edge_pts`` (difference in
    percentage points) and ``verdict``:

    * ``"coin flip"``  — difference < 5 points
    * ``"leans"``      — difference 5-15 points
    * ``"strongly"``   — difference >= 15 points

    The verdict is prefixed ``"against"`` when the edge is negative
    (target-first share below break-even).
    """
    decisive = target_first_pct + stop_first_pct
    if decisive <= 0:
        return {
            "decisive_target_pct": 0.0,
            "break_even_pct": 100.0 / (1.0 + reward_risk),
            "edge_pts": 0.0,
            "verdict": "coin flip",
            "note": "no decisive replays — nothing to judge",
        }
    decisive_target = 100.0 * target_first_pct / decisive
    break_even = 100.0 / (1.0 + reward_risk)
    edge = decisive_target - break_even
    magnitude = abs(edge)
    if magnitude < 5:
        label = "coin flip"
    elif magnitude < 15:
        label = "leans"
    else:
        label = "strongly"
    if edge < 0 and magnitude >= 5:
        label = f"{label} against"
    return {
        "decisive_target_pct": decisive_target,
        "break_even_pct": break_even,
        "edge_pts": edge,
        "verdict": label,
    }


# ---------------------------------------------------------------------------
# Data helpers: CSV input and synthetic demo bars (offline)
# ---------------------------------------------------------------------------

def bars_from_csv(path: str) -> list[Bar]:
    """Read 5-minute bars from a CSV with columns time,open,high,low,close."""
    bars: list[Bar] = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            bars.append(
                {
                    "time": row["time"],
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                }
            )
    return bars


def synthetic_bars(
    sessions: int = 30,
    start_price: float = 25_000.0,
    seed: int = 7,
) -> list[Bar]:
    """Deterministic synthetic 5m bars (random walk) — demo/offline use only.

    NOT real market data. Used so the module demo and CLI run offline.
    Each session is 75 bars, 09:15-15:30 IST, starting on a Monday and
    skipping weekends.
    """
    rng = random.Random(seed)
    bars: list[Bar] = []
    price = start_price
    day = date(2026, 10, 5)  # a Monday
    made = 0
    while made < sessions:
        if day.weekday() < 5:
            session_open = datetime.combine(day, time(9, 15), tzinfo=IST)
            for i in range(BARS_PER_SESSION):
                t = session_open + timedelta(minutes=5 * i)
                drift = rng.gauss(0.0, 0.0004)
                o = price
                c = price * math.exp(drift)
                h = max(o, c) * (1 + abs(rng.gauss(0, 0.0003)))
                low = min(o, c) * (1 - abs(rng.gauss(0, 0.0003)))
                bars.append({"time": t, "open": o, "high": h, "low": low, "close": c})
                price = c
            made += 1
        day += timedelta(days=1)
    return bars


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m bhanu.timemachine",
        description=(
            "Monte Carlo time machine + history replay for an NSE day trade. "
            "Replays the rest of the session 10,000 times from your entry/stop/target "
            "and re-runs the same rule on every past session. "
            "IMPORTANT: for an OPTIONS trade, pass bars of the OPTION PREMIUM series "
            "(--csv with premium bars) — never spot/underlying bars. Theta decay makes "
            "spot-bar replays meaningless for premium outcomes."
        ),
        epilog=(
            "This is a reality check, not proof of an edge. It ignores fees and "
            "slippage, and a gap through the stop is scored as exactly -1R. "
            "Paper trading only — nothing here places a real order."
        ),
    )
    parser.add_argument("--entry", type=float, required=True, help="planned entry price")
    parser.add_argument("--stop", type=float, required=True, help="stop-loss price (structural level)")
    parser.add_argument("--target", type=float, required=True, help="target price")
    parser.add_argument(
        "--direction", choices=["long", "short"], default="long",
        help="trade direction (default: long)",
    )
    parser.add_argument(
        "--csv", default=None,
        help="CSV of 5m bars (columns: time,open,high,low,close). "
        "For options trades this MUST be the option premium series, not spot. "
        "If omitted, synthetic demo bars are used (offline demo only).",
    )
    parser.add_argument("--n", type=int, default=DEFAULT_N, help="Monte Carlo replays (default: 10000)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="random seed (default: 19)")
    parser.add_argument(
        "--start-index", type=int, default=FIRST_TRADE_INDEX,
        help="bar index where the trade starts, 0 = 09:15 bar (default: 3, just after the 09:15-09:30 range)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.csv:
        bars = bars_from_csv(args.csv)
        source = args.csv
    else:
        bars = synthetic_bars()
        source = "synthetic demo bars (NOT real data)"
    print(f"Bars source: {source}")
    print(
        f"Setup: {args.direction} entry {args.entry} stop {args.stop} "
        f"target {args.target} (flat by 15:15 IST)"
    )
    try:
        mc = monte_carlo(
            bars, args.entry, args.stop, args.target,
            direction=args.direction, n=args.n, seed=args.seed,
            start_index=args.start_index,
        )
    except ValueError as exc:
        print(f"Cannot run: {exc}", file=sys.stderr)
        return 2
    hist = history_replay(bars, None, args.entry, args.stop, args.target, direction=args.direction)
    v = verdict(mc["target_first_pct"], mc["stop_first_pct"], mc["reward_risk"])
    print(f"\nMonte Carlo ({mc['n']} replays, seed {args.seed}):")
    print(f"  target first: {mc['target_first_pct']:.1f}%")
    print(f"  stop first:   {mc['stop_first_pct']:.1f}%")
    print(f"  neither:      {mc['neither_pct']:.1f}%")
    print(f"  avg R:        {mc['avg_r']:+.3f}")
    print(f"  reward/risk:  {mc['reward_risk']:.2f}R")
    print(
        f"\nVerdict: {v['verdict']} "
        f"(target-first among decisive: {v['decisive_target_pct']:.1f}%, "
        f"break-even: {v['break_even_pct']:.1f}%, edge: {v['edge_pts']:+.1f} pts)"
    )
    print(f"\nHistory replay ({hist['sessions']} sessions):")
    print(
        f"  fires: {hist['fires']}, target wins: {hist['target_wins']}, "
        f"stop losses: {hist['stop_losses']}, neither: {hist['neither']}, "
        f"avg R: {hist['avg_r']:+.3f}"
    )
    return 0


def demo() -> None:
    """Offline demo on synthetic bars (no network, no real data)."""
    bars = synthetic_bars()
    print("Time machine demo — synthetic bars only (NOT real market data).")
    print("Setup: long entry 25000, stop 24900, target 25200 (2R), flat by 15:15 IST.")
    print("NOTE: for an options trade, run this on the option PREMIUM series, never on spot bars.")
    mc = monte_carlo(bars, 25_000, 24_900, 25_200, direction="long")
    hist = history_replay(bars, None, 25_000, 24_900, 25_200, direction="long")
    v = verdict(mc["target_first_pct"], mc["stop_first_pct"], mc["reward_risk"])
    print(f"Monte Carlo: target first {mc['target_first_pct']:.1f}%, "
          f"stop first {mc['stop_first_pct']:.1f}%, neither {mc['neither_pct']:.1f}%, "
          f"avg R {mc['avg_r']:+.3f}")
    print(f"Verdict: {v['verdict']} (edge {v['edge_pts']:+.1f} pts vs break-even "
          f"{v['break_even_pct']:.1f}%)")
    print(f"History: {hist['fires']} fires, {hist['target_wins']} target wins, "
          f"{hist['stop_losses']} stop losses, {hist['neither']} neither, "
          f"avg R {hist['avg_r']:+.3f}")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        demo()
    else:
        raise SystemExit(main())
