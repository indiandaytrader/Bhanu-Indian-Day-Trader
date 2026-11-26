"""Risk limits and the pre-trade gate for NSE day trading.

Original code, written from the rules in the DayTrader study note
(``~/workspace/goals/trading-knowledge-research/files/DAY_TRADER_SKILL_STUDY.md``).
No code was copied from the day-trader-skill repo.

Two ideas carry over, adapted to NSE paper trading
--------------------------------------------------
1. **Four-limit sizing.** Quantity is the smallest of four whole-unit limits:
   max loss per trade / risk per unit; position cap / entry; max order /
   entry; cash available (budget less the cash buffer) / entry.
2. **Re-measure risk on a fresh price.** Risk is measured from the HIGHER of
   the planned entry and a fresh quote for longs (LOWER for shorts), so a
   stale plan can never understate the risk.

Daily-loss accounting counts the FULL max loss of every entry placed today,
win or lose — risk taken, not just realised losses.

Paper trading only: nothing here places an order.  A gate verdict of
``"BLOCK"`` is final for that order; exits are never blocked by day limits
(see :func:`gate_exit`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

ENTRY_WINDOW_START = dtime(9, 30)   # no entries before 09:30 IST
ENTRY_WINDOW_END = dtime(15, 0)     # no new entries after 15:00 IST


@dataclass
class RiskConfig:
    """Risk limits for one trading account (paper by default)."""

    budget: float = 100_000.0        # total account size (₹)
    max_loss_per_trade: float = 500.0  # max ₹ lost if the stop is hit
    max_daily_loss: float = 1_500.0    # max ₹ of full max-losses per day
    max_trades_per_day: int = 3
    min_rr: float = 1.5              # minimum reward:risk to take a trade
    target_rr: float = 2.0           # planned R:R when no target is given
    position_cap_pct: float = 0.75   # at most 75% of budget in one position
    max_order: float = 80_000.0      # max ₹ value of a single order (80% of budget)
    cash_buffer_pct: float = 0.10    # keep 10% of budget in cash
    allowed_assets: frozenset = frozenset({"index", "option"})
    entry_window_start: dtime = ENTRY_WINDOW_START
    entry_window_end: dtime = ENTRY_WINDOW_END
    flat_by: str = "15:15"


def default_config(**overrides: Any) -> RiskConfig:
    """A :class:`RiskConfig` with the defaults above, plus any overrides."""
    return RiskConfig(**overrides)


def _risk_per_unit(entry: float, stop: float) -> float:
    return abs(entry - stop)


def size_position(
    entry: float,
    stop: float,
    config: RiskConfig,
    target: Optional[float] = None,
) -> int:
    """Whole-unit size for a trade — the smallest of four limits.

    ``qty = min(max_loss_per_trade / risk_per_unit,
    position_cap / entry, max_order / entry, cash_available / entry)``,
    floored to whole units, where ``position_cap = budget * position_cap_pct``
    and ``cash_available = budget * (1 - cash_buffer_pct)``.

    Returns 0 (refuse) when the risk per unit is zero/negative, when the
    R:R is below ``config.min_rr``, or when every limit floors to 0 units.
    R:R comes from ``target`` when given, otherwise from
    ``config.target_rr`` (the planned multiple).
    """
    if entry <= 0 or stop <= 0:
        return 0
    risk = _risk_per_unit(entry, stop)
    if risk <= 0:
        return 0
    if target is not None:
        rr = abs(target - entry) / risk
    else:
        rr = config.target_rr
    if rr < config.min_rr:
        return 0

    position_cap = config.budget * config.position_cap_pct
    cash_available = config.budget * (1.0 - config.cash_buffer_pct)
    limits = (
        config.max_loss_per_trade / risk,
        position_cap / entry,
        config.max_order / entry,
        cash_available / entry,
    )
    qty = int(min(limits))  # floor to whole units
    return max(qty, 0)


def remeasure_risk(
    plan_entry: float,
    fresh_price: float,
    stop: float,
    direction: str,
    config: RiskConfig,
    target: Optional[float] = None,
) -> dict:
    """Re-size a planned trade against a fresh price.

    Risk is measured from the HIGHER of the plan entry and the fresh price
    for longs (LOWER for shorts) — a stale plan must not understate risk.
    If the price ran away, the same stop means more risk per unit, so the
    size shrinks; if R:R falls below ``config.min_rr`` the trade is refused.

    Returns a dict with ``entry`` (the effective entry used), ``risk_per_unit``,
    ``qty``, ``rr``, ``refused`` and ``reason``.
    """
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")
    effective_entry = (
        max(plan_entry, fresh_price) if direction == "long"
        else min(plan_entry, fresh_price)
    )
    risk = _risk_per_unit(effective_entry, stop)
    if risk <= 0:
        return {
            "entry": effective_entry, "risk_per_unit": risk, "qty": 0,
            "rr": 0.0, "refused": True,
            "reason": "fresh price crossed the stop — risk is zero or negative",
        }
    if target is not None:
        rr = abs(target - effective_entry) / risk
    else:
        rr = config.target_rr
    qty = size_position(effective_entry, stop, config, target=target)
    if rr < config.min_rr:
        return {
            "entry": effective_entry, "risk_per_unit": risk, "qty": 0,
            "rr": rr, "refused": True,
            "reason": f"R:R {rr:.2f} below minimum {config.min_rr:.2f} at the fresh price",
        }
    if qty == 0:
        return {
            "entry": effective_entry, "risk_per_unit": risk, "qty": 0,
            "rr": rr, "refused": True,
            "reason": "four-limit sizing gives 0 units at the fresh price",
        }
    return {
        "entry": effective_entry, "risk_per_unit": risk, "qty": qty,
        "rr": rr, "refused": False, "reason": "ok",
    }


def _max_loss_of(entry_record: Any) -> float:
    """Full max loss of one entry placed today.

    Accepts a dict (``max_loss`` key, or ``qty``/``entry``/``stop`` to
    compute ``qty * |entry - stop|``) or an object with the same attributes.
    """
    if isinstance(entry_record, dict):
        get = entry_record.get
    else:
        get = lambda k, d=None: getattr(entry_record, k, d)  # noqa: E731
    explicit = get("max_loss")
    if explicit is not None:
        return float(explicit)
    qty = get("qty")
    entry = get("entry")
    stop = get("stop")
    if qty is not None and entry is not None and stop is not None:
        return float(qty) * abs(float(entry) - float(stop))
    raise ValueError(
        "entry record needs 'max_loss' or qty/entry/stop to compute max loss"
    )


def daily_loss_used(entries_today: Iterable[Any]) -> float:
    """Sum of the FULL max loss of every entry placed today, win or lose."""
    return sum(_max_loss_of(e) for e in entries_today)


def _order_time(order: dict) -> Optional[dtime]:
    """Entry time of an order as a time-of-day (IST), if one is given."""
    raw = order.get("time")
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.astimezone(IST).time()
    if isinstance(raw, dtime):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.strptime(raw.strip(), "%H:%M").time()
        except ValueError:
            return None
    return None


def _max_loss_for(order: dict) -> Optional[float]:
    qty = order.get("qty")
    entry = order.get("entry")
    stop = order.get("stop")
    if qty is None or entry is None or stop is None:
        return None
    return float(qty) * abs(float(entry) - float(stop))


def gate(
    order: dict,
    config: RiskConfig,
    day_state: dict,
) -> tuple[str, str]:
    """Pre-trade gate: ``("PASS", reason)`` or ``("BLOCK", reason)``.

    Checks, in order: format, asset (indices/options only per config),
    size > 0, position cap, cash buffer, stop required and on the right
    side, loss at stop <= max loss/trade, R:R >= min, daily loss (full max
    losses) <= max daily loss, trades/day <= max, entry window 09:30-15:00
    IST, duplicates.  A ``"BLOCK"`` is final for that order.

    ``order`` keys: ``side`` ("buy"/"sell"), ``asset`` ("index"/"option"),
    ``symbol``, ``direction`` ("long"/"short"), ``qty``, ``entry``,
    ``stop``, ``target`` (optional), ``time`` (optional, IST), ``id``
    (optional, for duplicate detection).

    ``day_state`` keys: ``entries_today`` (list of entry records, see
    :func:`daily_loss_used`), ``trades_today`` (int), ``open_orders``
    (list of order ids or ``(symbol, direction)`` pairs already working).
    """
    # 1. Format.
    required = ("side", "asset", "symbol", "direction", "qty", "entry")
    missing = [k for k in required if order.get(k) is None]
    if missing:
        return "BLOCK", f"missing fields: {', '.join(missing)}"
    if order["side"] not in ("buy", "sell"):
        return "BLOCK", f"bad side {order['side']!r}: use 'buy' or 'sell'"
    if order["direction"] not in ("long", "short"):
        return "BLOCK", f"bad direction {order['direction']!r}"
    try:
        qty = int(order["qty"])
        entry = float(order["entry"])
    except (TypeError, ValueError):
        return "BLOCK", "qty/entry are not numeric"
    if qty != order["qty"] and not float(order["qty"]).is_integer():
        return "BLOCK", "qty must be a whole number of units"
    if entry <= 0:
        return "BLOCK", f"entry must be > 0, got {entry}"

    # 2. Asset — indices/options only per config.
    if order["asset"] not in config.allowed_assets:
        return "BLOCK", (
            f"asset {order['asset']!r} not allowed "
            f"(allowed: {sorted(config.allowed_assets)})"
        )

    # 3. Size.
    if qty <= 0:
        return "BLOCK", f"qty must be > 0, got {qty}"

    # 4. Position cap.
    position_value = qty * entry
    position_cap = config.budget * config.position_cap_pct
    if position_value > position_cap:
        return "BLOCK", (
            f"position value ₹{position_value:,.2f} exceeds position cap "
            f"₹{position_cap:,.2f}"
        )

    # 5. Cash buffer.
    cash_available = config.budget * (1.0 - config.cash_buffer_pct)
    if position_value > cash_available:
        return "BLOCK", (
            f"position value ₹{position_value:,.2f} exceeds cash available "
            f"₹{cash_available:,.2f} (budget less {config.cash_buffer_pct:.0%} buffer)"
        )

    # 6. Stop required, on the right side.
    stop = order.get("stop")
    if stop is None:
        return "BLOCK", "stop is required — no trade without a stop"
    stop = float(stop)
    if order["direction"] == "long" and stop >= entry:
        return "BLOCK", f"long stop {stop:.2f} is not below entry {entry:.2f}"
    if order["direction"] == "short" and stop <= entry:
        return "BLOCK", f"short stop {stop:.2f} is not above entry {entry:.2f}"

    # 7. Loss at stop <= max loss/trade.
    max_loss = _max_loss_for(order)
    if max_loss is not None and max_loss > config.max_loss_per_trade:
        return "BLOCK", (
            f"loss at stop ₹{max_loss:,.2f} exceeds max loss/trade "
            f"₹{config.max_loss_per_trade:,.2f}"
        )

    # 8. R:R >= min.
    target = order.get("target")
    if target is not None:
        risk = abs(entry - stop)
        rr = abs(float(target) - entry) / risk if risk > 0 else 0.0
        if rr < config.min_rr:
            return "BLOCK", f"R:R {rr:.2f} below minimum {config.min_rr:.2f}"

    # 9. Daily loss (full max losses of entries placed today, plus this one).
    entries_today = day_state.get("entries_today", [])
    used = daily_loss_used(entries_today)
    this_loss = max_loss or 0.0
    if used + this_loss > config.max_daily_loss:
        return "BLOCK", (
            f"daily loss would be ₹{used + this_loss:,.2f} "
            f"(used ₹{used:,.2f} + this ₹{this_loss:,.2f}), "
            f"max daily loss ₹{config.max_daily_loss:,.2f}"
        )

    # 10. Trades/day.
    if day_state.get("trades_today", 0) >= config.max_trades_per_day:
        return "BLOCK", (
            f"already {day_state.get('trades_today')} trade(s) today, "
            f"max {config.max_trades_per_day}"
        )

    # 11. Entry window 09:30-15:00 IST.
    when = _order_time(order)
    if when is not None and not (
        config.entry_window_start <= when <= config.entry_window_end
    ):
        return "BLOCK", (
            f"entry time {when.strftime('%H:%M')} outside window "
            f"{config.entry_window_start.strftime('%H:%M')}-"
            f"{config.entry_window_end.strftime('%H:%M')} IST"
        )

    # 12. Duplicates.
    open_orders = day_state.get("open_orders", [])
    order_id = order.get("id")
    if order_id is not None and order_id in open_orders:
        return "BLOCK", f"duplicate order id {order_id!r} already working"
    key = (order["symbol"], order["direction"])
    if key in open_orders:
        return "BLOCK", f"duplicate: {order['symbol']} {order['direction']} already working"

    return "PASS", "ok"


def gate_exit(order: dict, config: RiskConfig, day_state: dict) -> tuple[str, str]:
    """Gate for an exit order.  Exits are never blocked by day limits —
    only the format and asset checks apply, so a trader can always get
    out (stop, target or flat-by square-off)."""
    required = ("side", "asset", "symbol", "direction", "qty")
    missing = [k for k in required if order.get(k) is None]
    if missing:
        return "BLOCK", f"missing fields: {', '.join(missing)}"
    if order["side"] not in ("buy", "sell"):
        return "BLOCK", f"bad side {order['side']!r}: use 'buy' or 'sell'"
    if order["direction"] not in ("long", "short"):
        return "BLOCK", f"bad direction {order['direction']!r}"
    if order["asset"] not in config.allowed_assets:
        return "BLOCK", (
            f"asset {order['asset']!r} not allowed "
            f"(allowed: {sorted(config.allowed_assets)})"
        )
    try:
        qty = int(order["qty"])
    except (TypeError, ValueError):
        return "BLOCK", "qty is not numeric"
    if qty <= 0:
        return "BLOCK", f"qty must be > 0, got {qty}"
    return "PASS", "ok"


if __name__ == "__main__":
    # Offline demo on synthetic numbers (not real prices).
    cfg = default_config()
    print("config:", cfg)

    qty = size_position(entry=226.55, stop=176.55, config=cfg)
    print(f"size_position(entry=226.55, stop=176.55) = {qty} units")

    # Fresh price ran up after the plan: risk is re-measured from the
    # higher price, so the size shrinks.
    print(remeasure_risk(226.55, 240.00, 176.55, "long", cfg, target=326.55))
    print(remeasure_risk(226.55, 226.55, 176.55, "long", cfg, target=326.55))

    print("daily_loss_used:", daily_loss_used([
        {"max_loss": 500.0}, {"qty": 2, "entry": 226.55, "stop": 176.55},
    ]))

    day_state = {"entries_today": [{"max_loss": 500.0}], "trades_today": 1, "open_orders": []}
    order = {
        "side": "buy", "asset": "option", "symbol": "NIFTY25800PE",
        "direction": "long", "qty": 1, "entry": 226.55, "stop": 176.55,
        "target": 326.55, "time": "10:15", "id": "ord-1",
    }
    print("gate:", gate(order, cfg, day_state))
    print("gate (bad asset):", gate({**order, "asset": "crypto"}, cfg, day_state))
    print("gate (no stop):", gate({k: v for k, v in order.items() if k != "stop"}, cfg, day_state))
    print("gate_exit (after daily limit used up):",
          gate_exit({"side": "sell", "asset": "option", "symbol": "NIFTY25800PE",
                     "direction": "long", "qty": 1}, cfg,
                    {"entries_today": [{"max_loss": 1500.0}], "trades_today": 3}))
