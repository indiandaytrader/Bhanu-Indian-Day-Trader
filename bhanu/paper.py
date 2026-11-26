"""Paper-trading ledger — paper execution only.

This module REPLACES DayTrader's trader-broker: paper execution only.
DayTrader's trader-broker places real orders through Alpaca / IBKR, which
are US-only brokers. Those are NOT used here — there is no live broker, no
broker API, no API keys and no real order anywhere in this module (or in
this repo). Every position opened here is a paper position recorded in a
local JSON file; nothing leaves this machine.

State
-----
All state lives in one JSON file (default ``data/paper.json`` next to the
``bhanu`` package, overridable per call with ``state_path``). The file holds
open positions, closed trades and a sequence counter. Writes are atomic
(write to a temp file, then ``os.replace``) so a crash cannot leave a
half-written ledger.

Money
-----
All money is in INR. ``qty`` is in the instrument's own units (e.g. option
lots / contracts as entered — this module does not know lot sizes; the
caller passes the total quantity). P&L is ``qty * price move`` in INR.

Daily accounting
----------------
Risk taken on a day counts the FULL max loss of every entry placed that
day, win or lose — a trade that later wins still used up its risk budget
for the day. ``day_summary`` reports exactly that, alongside realised P&L
and whether the book is flat.

Rules enforced here
-------------------
* A long needs stop < entry < target; a short needs target < entry < stop.
* Quantity and prices must be positive.
* A stop is never moved once the position is open (there is no function
  that edits a stop — close the position and open a new one instead).
* Flat by 15:15 IST: ``flat_all`` closes every open position at the prices
  given; it never invents an exit price.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence, Union

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9 fallback, unused on 3.10+
    ZoneInfo = None  # type: ignore[assignment]
from datetime import timezone, timedelta

IST = ZoneInfo("Asia/Kolkata") if ZoneInfo is not None else timezone(timedelta(hours=5, minutes=30))

#: Default state file: <repo root>/data/paper.json (next to the ``bhanu`` package).
DEFAULT_STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "paper.json"

#: Default reason used when squaring off for the day.
FLAT_REASON = "15:15 flat"

DateLike = Union[str, date, datetime]

__all__ = [
    "IST",
    "DEFAULT_STATE_PATH",
    "FLAT_REASON",
    "open_position",
    "close_position",
    "list_positions",
    "list_trades",
    "day_summary",
    "flat_all",
    "load_state",
    "save_state",
]


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------

def _resolve_path(state_path: Optional[Union[str, Path]]) -> Path:
    if state_path is None:
        return DEFAULT_STATE_PATH
    return Path(state_path)


def _empty_state() -> dict:
    return {"positions": [], "trades": [], "seq": 0}


def load_state(state_path: Optional[Union[str, Path]] = None) -> dict:
    """Load the ledger from its JSON file; return an empty ledger if missing."""
    path = _resolve_path(state_path)
    if not path.exists():
        return _empty_state()
    with open(path, encoding="utf-8") as fh:
        state = json.load(fh)
    # Tolerate hand-edited or older files missing keys.
    state.setdefault("positions", [])
    state.setdefault("trades", [])
    state.setdefault("seq", len(state["trades"]) + len(state["positions"]))
    return state


def save_state(state: dict, state_path: Optional[Union[str, Path]] = None) -> Path:
    """Atomically write the ledger JSON (temp file + os.replace)."""
    path = _resolve_path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def _now_iso() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def _day_of(iso_ts: str) -> str:
    """``YYYY-MM-DD`` (IST) for an ISO timestamp stored in the ledger."""
    return datetime.fromisoformat(iso_ts).astimezone(IST).date().isoformat()


def _as_day(value: DateLike) -> str:
    if isinstance(value, datetime):
        return value.astimezone(IST).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    # Accept "YYYY-MM-DD" or a full ISO timestamp.
    return text[:10] if "T" in text else text


# ---------------------------------------------------------------------------
# Validation / P&L
# ---------------------------------------------------------------------------

def _validate_setup(direction: str, qty: float, entry: float, stop: float, target: float) -> None:
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")
    if qty <= 0:
        raise ValueError("qty must be positive")
    for name, value in (("entry", entry), ("stop", stop), ("target", target)):
        if value <= 0:
            raise ValueError(f"{name} must be a positive price, got {value!r}")
    if direction == "long" and not stop < entry < target:
        raise ValueError("for a long: stop < entry < target is required")
    if direction == "short" and not target < entry < stop:
        raise ValueError("for a short: target < entry < stop is required")


def _max_loss(direction: str, qty: float, entry: float, stop: float) -> float:
    """Full max loss in INR if the stop is hit (qty x distance to stop)."""
    return float(qty) * abs(float(entry) - float(stop))


def _realised_pnl(direction: str, qty: float, entry: float, exit_price: float) -> float:
    """Realised P&L in INR at ``exit_price``."""
    if direction == "long":
        return float(qty) * (float(exit_price) - float(entry))
    return float(qty) * (float(entry) - float(exit_price))


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

def open_position(
    symbol: str,
    direction: str,
    qty: float,
    entry: float,
    stop: float,
    target: float,
    note: str = "",
    state_path: Optional[Union[str, Path]] = None,
) -> dict:
    """Open a PAPER position and record it in the ledger.

    This never places a real order — it only appends a paper position to
    the JSON state file. Returns the position dict, which includes
    ``max_loss``: the full max loss (INR) if the stop is hit. That figure
    is what daily risk accounting counts, win or lose.
    """
    _validate_setup(direction, qty, entry, stop, target)
    state = load_state(state_path)
    state["seq"] = int(state.get("seq", 0)) + 1
    position = {
        "id": f"P-{state['seq']:04d}",
        "symbol": str(symbol),
        "direction": direction,
        "qty": float(qty),
        "entry": float(entry),
        "stop": float(stop),
        "target": float(target),
        "note": note,
        "status": "open",
        "opened_at": _now_iso(),
        "max_loss": _max_loss(direction, qty, entry, stop),
    }
    state["positions"].append(position)
    save_state(state, state_path)
    return dict(position)


def close_position(
    pos_id: str,
    exit_price: float,
    reason: str,
    state_path: Optional[Union[str, Path]] = None,
) -> dict:
    """Close a paper position at ``exit_price``; return the closed trade.

    The returned trade dict includes ``realised_pnl`` (INR) and
    ``r_multiple`` (realised P&L divided by the position's full max loss).
    Raises ``KeyError`` if no open position has that id, ``ValueError`` if
    the exit price is not positive.
    """
    if exit_price <= 0:
        raise ValueError(f"exit_price must be positive, got {exit_price!r}")
    state = load_state(state_path)
    position = next(
        (p for p in state["positions"] if p["id"] == pos_id and p["status"] == "open"),
        None,
    )
    if position is None:
        raise KeyError(f"no open position with id {pos_id!r}")
    pnl = _realised_pnl(position["direction"], position["qty"], position["entry"], exit_price)
    trade = {
        **position,
        "status": "closed",
        "exit_price": float(exit_price),
        "exit_reason": reason,
        "closed_at": _now_iso(),
        "realised_pnl": pnl,
        "r_multiple": (pnl / position["max_loss"]) if position["max_loss"] else 0.0,
    }
    state["positions"] = [p for p in state["positions"] if p["id"] != pos_id]
    state["trades"].append(trade)
    save_state(state, state_path)
    return dict(trade)


def list_positions(state_path: Optional[Union[str, Path]] = None) -> list[dict]:
    """All currently open paper positions (copies, oldest first)."""
    return [dict(p) for p in load_state(state_path)["positions"]]


def list_trades(state_path: Optional[Union[str, Path]] = None) -> list[dict]:
    """All closed paper trades (copies, oldest first)."""
    return [dict(t) for t in load_state(state_path)["trades"]]


# ---------------------------------------------------------------------------
# Day summary / flat
# ---------------------------------------------------------------------------

def day_summary(day: DateLike, state_path: Optional[Union[str, Path]] = None) -> dict:
    """Summary for one day (IST): trades, realised P&L, risk taken, flat?

    * ``trades_today`` — trades closed on this day.
    * ``realised_pnl`` — sum of realised P&L (INR) of those trades.
    * ``risk_taken`` — sum of the FULL max losses of every entry placed
      on this day, win or lose (open positions opened today + trades
      opened today, even if already closed).
    * ``flat`` — True when no position is open.
    * ``open_positions`` — number of open positions (any day).
    """
    day_str = _as_day(day)
    state = load_state(state_path)
    trades_today = [t for t in state["trades"] if _day_of(t["closed_at"]) == day_str]
    entries_today = [
        p for p in state["positions"] if _day_of(p["opened_at"]) == day_str
    ] + [
        t for t in state["trades"] if _day_of(t["opened_at"]) == day_str
    ]
    return {
        "date": day_str,
        "trades_today": len(trades_today),
        "realised_pnl": sum(t["realised_pnl"] for t in trades_today),
        "risk_taken": sum(p["max_loss"] for p in entries_today),
        "open_positions": len(state["positions"]),
        "flat": len(state["positions"]) == 0,
    }


def flat_all(
    prices: Mapping[str, float],
    reason: str = FLAT_REASON,
    state_path: Optional[Union[str, Path]] = None,
) -> dict:
    """Close every open position at the given prices (flat by 15:15 IST).

    ``prices`` maps symbol -> exit price. A position whose symbol has no
    price in ``prices`` is left open and listed under ``"unclosed"`` — an
    exit price is never invented. Returns ``{"closed": [...], "unclosed":
    [...]}`` with the closed trade dicts and the ids left open.
    """
    closed: list[dict] = []
    unclosed: list[str] = []
    for position in list_positions(state_path):
        price = prices.get(position["symbol"])
        if price is None:
            unclosed.append(position["id"])
            continue
        closed.append(close_position(position["id"], float(price), reason, state_path))
    return {"closed": closed, "unclosed": unclosed}


# ---------------------------------------------------------------------------
# Offline demo (no network, uses a throwaway temp state file)
# ---------------------------------------------------------------------------

def demo() -> None:
    """Offline demo on a temporary state file (the real ledger is untouched)."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        demo_path = Path(tmp) / "paper.json"
        print("Paper trading demo — paper execution only, no broker, no real orders.")
        pos = open_position(
            "NIFTY 25800 CE", "long", qty=65, entry=120.0, stop=90.0,
            target=180.0, note="demo ORB breakout", state_path=demo_path,
        )
        print(f"Opened {pos['id']}: {pos['symbol']} long 65 @ 120.0, "
              f"stop 90.0, target 180.0, max loss ₹{pos['max_loss']:.2f}")
        print(f"Open positions: {len(list_positions(demo_path))}")
        trade = close_position(pos["id"], 165.0, "target hit (demo)", state_path=demo_path)
        print(f"Closed {trade['id']} @ 165.0: realised P&L ₹{trade['realised_pnl']:.2f} "
              f"({trade['r_multiple']:+.2f}R)")
        summary = day_summary(date.today(), state_path=demo_path)
        print(f"Day summary: {summary['trades_today']} trade(s), "
              f"realised ₹{summary['realised_pnl']:.2f}, "
              f"risk taken ₹{summary['risk_taken']:.2f}, flat: {summary['flat']}")
        print("Demo state file was temporary and is now deleted.")


if __name__ == "__main__":
    demo()
