"""Exit planner for NSE day trading: stop + target set BEFORE entry.

Original code, written from the rules in the DayTrader study note
(``~/workspace/goals/trading-knowledge-research/files/DAY_TRADER_SKILL_STUDY.md``).
No code was copied from the day-trader-skill repo or from Market Lab.

Rules
-----
* Stops (``rule``):
  - ``"or-mid"``  (default): the opening-range midpoint.
  - ``"or-low"`` / ``"or-high"``: the range extreme.
  - ``"atr:K"``: K x 14-bar ATR under the entry (long) / above it (short).
  - ``"pct:X"``: X% under the entry (long) / above it (short).
* Direction is inferred from the entry vs the range midpoint: an entry above
  the midpoint is a long, below it a short.  An entry exactly at the
  midpoint is ambiguous and is refused.
* Refusals (``ValueError`` with a reason): stop on the wrong side of the
  entry, target on the wrong side of the entry, or risk under 0.05% of the
  entry price (too tight for the spread).
* Rounding: the stop rounds *away* from the entry, the target rounds
  half-up (both to 2 decimals, i.e. paise).
* A stop is never moved further once the trade is on — see
  :func:`check_stop_move`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Optional

MIN_RISK_PCT = 0.0005  # 0.05% of entry price — tighter than this is spread noise


@dataclass
class ExitPlan:
    """Stop + target for one planned trade."""

    entry: float
    stop: float
    target: float
    stop_rule: str
    risk_per_unit: float
    reward_per_unit: float
    rr: float
    flat_by: str = "15:15"
    direction: str = "long"


def _round_away_from_entry(value: float, entry: float) -> float:
    """Round to 2 decimals away from the entry price.

    Long stop: round down (further below the entry).  Short stop: round up
    (further above the entry).  The stop must never be rounded *towards*
    the entry, which would understate the risk.
    """
    cents = Decimal("0.01")
    d = Decimal(str(value))
    if value < entry:  # long stop — round down
        return float(d.quantize(cents, rounding=ROUND_FLOOR))
    return float(d.quantize(cents, rounding=ROUND_CEILING))


def _round_half_up(value: float) -> float:
    """Round to 2 decimals, halves away from zero (target rounding)."""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _parse_rule(rule: str) -> tuple[str, Optional[float]]:
    """Split a stop rule into (kind, parameter)."""
    if rule in ("or-mid", "or-low", "or-high"):
        return rule, None
    if rule.startswith("atr:"):
        try:
            k = float(rule.split(":", 1)[1])
        except ValueError:
            raise ValueError(f"bad ATR stop rule {rule!r}: expected 'atr:K'") from None
        if k <= 0:
            raise ValueError(f"bad ATR stop rule {rule!r}: K must be > 0")
        return "atr", k
    if rule.startswith("pct:"):
        try:
            x = float(rule.split(":", 1)[1])
        except ValueError:
            raise ValueError(f"bad percent stop rule {rule!r}: expected 'pct:X'") from None
        if x <= 0:
            raise ValueError(f"bad percent stop rule {rule!r}: X must be > 0")
        return "pct", x
    raise ValueError(
        f"unknown stop rule {rule!r}: use 'or-mid', 'or-low', 'or-high', "
        "'atr:K' or 'pct:X'"
    )


def compute_exit(
    entry: float,
    range_high: float,
    range_low: float,
    atr: Optional[float] = None,
    rule: str = "or-mid",
    target_rr: float = 2.0,
    flat_by: str = "15:15",
) -> ExitPlan:
    """Compute stop + target for a planned entry.

    Parameters
    ----------
    entry:
        Planned entry price.
    range_high, range_low:
        Opening-range extremes (09:15-09:30 IST).
    atr:
        14-bar ATR, required for the ``"atr:K"`` rule.
    rule:
        Stop rule — ``"or-mid"`` (default), ``"or-low"``, ``"or-high"``,
        ``"atr:K"`` or ``"pct:X"``.
    target_rr:
        Reward-to-risk multiple for the target (default 2.0).
    flat_by:
        Square-off time, ``"15:15"`` IST by default.

    Raises
    ------
    ValueError
        When the stop or target lands on the wrong side of the entry, when
        the risk is under 0.05% of the entry price, or when the rule/inputs
        are invalid.  The message states the reason.
    """
    if entry <= 0:
        raise ValueError(f"entry must be > 0, got {entry}")
    if range_high <= range_low:
        raise ValueError(
            f"range_high ({range_high}) must be above range_low ({range_low})"
        )
    if target_rr <= 0:
        raise ValueError(f"target_rr must be > 0, got {target_rr}")
    if atr is not None and atr <= 0:
        raise ValueError(f"atr must be > 0, got {atr}")

    midpoint = (range_high + range_low) / 2.0
    if entry == midpoint:
        raise ValueError(
            "entry exactly at the range midpoint: direction is ambiguous, "
            "cannot place a stop"
        )
    direction = "long" if entry > midpoint else "short"
    sign = 1.0 if direction == "long" else -1.0  # stop sits entry - sign*risk

    kind, param = _parse_rule(rule)
    if kind == "or-mid":
        raw_stop = midpoint
    elif kind == "or-low":
        raw_stop = range_low
    elif kind == "or-high":
        raw_stop = range_high
    elif kind == "atr":
        if atr is None:
            raise ValueError("rule 'atr:K' needs atr= (14-bar ATR)")
        raw_stop = entry - sign * param * atr
    else:  # pct
        raw_stop = entry * (1.0 - sign * param / 100.0)

    stop = _round_away_from_entry(raw_stop, entry)
    # Keep money figures at paise precision (avoids float noise in R:R).
    risk_per_unit = round(abs(entry - stop), 2)

    if direction == "long" and stop >= entry:
        raise ValueError(
            f"stop {stop:.2f} is not below entry {entry:.2f} for a long "
            f"(rule {rule!r})"
        )
    if direction == "short" and stop <= entry:
        raise ValueError(
            f"stop {stop:.2f} is not above entry {entry:.2f} for a short "
            f"(rule {rule!r})"
        )
    if risk_per_unit < MIN_RISK_PCT * entry:
        raise ValueError(
            f"risk per unit {risk_per_unit:.2f} is under 0.05% of entry "
            f"{entry:.2f} ({MIN_RISK_PCT * entry:.2f}) — too tight for the spread"
        )

    target = _round_half_up(entry + sign * risk_per_unit * target_rr)
    reward_per_unit = round(abs(target - entry), 2)
    if direction == "long" and target <= entry:
        raise ValueError(f"target {target:.2f} is not above entry {entry:.2f}")
    if direction == "short" and target >= entry:
        raise ValueError(f"target {target:.2f} is not below entry {entry:.2f}")

    return ExitPlan(
        entry=entry,
        stop=stop,
        target=target,
        stop_rule=rule,
        risk_per_unit=risk_per_unit,
        reward_per_unit=reward_per_unit,
        rr=reward_per_unit / risk_per_unit if risk_per_unit else 0.0,
        flat_by=flat_by,
        direction=direction,
    )


def check_stop_move(old_stop: float, new_stop: float, direction: str) -> bool:
    """True only if the new stop does NOT widen the trade.

    A stop may be tightened (long: moved up; short: moved down) or left
    alone, but it is never moved further away once the trade is on.
    """
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")
    if direction == "long":
        return new_stop >= old_stop
    return new_stop <= old_stop


if __name__ == "__main__":
    # Offline demo on synthetic levels (not real prices).
    for rule, kwargs in [
        ("or-mid", {}),
        ("or-low", {}),
        ("atr:1.5", {"atr": 40.0}),
        ("pct:0.5", {}),
    ]:
        try:
            plan = compute_exit(
                entry=25730.0, range_high=25718.0, range_low=25687.0,
                rule=rule, **kwargs,
            )
            print(f"{rule:8} -> {plan}")
        except ValueError as exc:
            print(f"{rule:8} -> refused: {exc}")

    # Short side.
    print(compute_exit(entry=25680.0, range_high=25718.0, range_low=25687.0))

    # Refusals.
    for bad in [
        dict(entry=25702.5, range_high=25718.0, range_low=25687.0),  # midpoint
        dict(entry=25730.0, range_high=25718.0, range_low=25687.0, rule="pct:0.01"),
        dict(entry=25730.0, range_high=25718.0, range_low=25687.0, rule="atr:1.5"),
    ]:
        try:
            compute_exit(**bad)
        except ValueError as exc:
            print(f"refused: {exc}")

    print("tighten long stop 25690 -> 25700:", check_stop_move(25690, 25700, "long"))
    print("widen   long stop 25690 -> 25680:", check_stop_move(25690, 25680, "long"))
    print("tighten short stop 25720 -> 25710:", check_stop_move(25720, 25710, "short"))
    print("widen   short stop 25720 -> 25730:", check_stop_move(25720, 25730, "short"))
