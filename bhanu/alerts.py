"""Stop / target / flat-by alerts for an open paper trade (NSE port).

Port of the DayTrader ``trader-alerts`` rules, rewritten from the study
note for NSE hours. Each alert rings ONCE: once an alert has fired it is
never returned again (the caller keeps a ``fired`` set of alert ids).

Alerts are built from the trade plan BEFORE entry: a stop alert, a
target alert and a flat-by alert (square off by 15:15 IST, intraday
only). Helpers add above/below price alerts, percentage-move alerts
and time alerts.

No network, no external dependencies. All times are Asia/Kolkata
(``zoneinfo.ZoneInfo("Asia/Kolkata")``). Prices are checked against the
levels the caller supplies — this module never fetches a quote and
never presents a price as live.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_FLAT_BY = "15:15"


@dataclass
class AlertPlan:
    """One open paper trade's alert plan. All prices are INR per unit."""

    symbol: str
    direction: str  # "long" or "short"
    stop: float
    target: float
    flat_by: str = DEFAULT_FLAT_BY  # "HH:MM" IST
    entry: float | None = None

    def __post_init__(self) -> None:
        self.direction = self.direction.lower()
        if self.direction not in ("long", "short"):
            raise ValueError(
                f"direction must be 'long' or 'short', got {self.direction!r}"
            )
        if self.stop <= 0 or self.target <= 0:
            raise ValueError("stop and target must be positive")
        if self.direction == "long" and self.stop >= self.target:
            raise ValueError("long: stop must be below target")
        if self.direction == "short" and self.stop <= self.target:
            raise ValueError("short: stop must be above target")
        if self.entry is not None and self.entry <= 0:
            raise ValueError("entry must be positive")
        parse_hhmm(self.flat_by)  # validates the "HH:MM" format


def parse_hhmm(hhmm: str) -> dtime:
    """Parse an "HH:MM" string into a ``datetime.time`` (raises ValueError)."""
    parts = hhmm.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError(f"time must be 'HH:MM', got {hhmm!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"time out of range: {hhmm!r}")
    return dtime(hour, minute)


def _alert_id(kind: str, level: str) -> str:
    return f"{kind}:{level}"


def build_alerts(plan: AlertPlan) -> list[dict]:
    """Build the stop / target / flat-by alert triplet for a plan.

    Each alert is a dict with ``id``, ``kind`` ("stop", "target" or
    "flat_by"), ``symbol``, ``direction`` and either ``level`` (price)
    or ``time`` ("HH:MM" IST).
    """
    return [
        {
            "id": _alert_id("stop", f"{plan.stop:.2f}"),
            "kind": "stop",
            "symbol": plan.symbol,
            "direction": plan.direction,
            "level": plan.stop,
        },
        {
            "id": _alert_id("target", f"{plan.target:.2f}"),
            "kind": "target",
            "symbol": plan.symbol,
            "direction": plan.direction,
            "level": plan.target,
        },
        {
            "id": _alert_id("flat_by", plan.flat_by),
            "kind": "flat_by",
            "symbol": plan.symbol,
            "direction": plan.direction,
            "time": plan.flat_by,
        },
    ]


def price_alert(symbol: str, kind: str, level: float) -> dict:
    """An above/below price alert. ``kind`` is "above" or "below"."""
    if kind not in ("above", "below"):
        raise ValueError(f"kind must be 'above' or 'below', got {kind!r}")
    if level <= 0:
        raise ValueError("level must be positive")
    return {
        "id": _alert_id(kind, f"{level:.2f}"),
        "kind": kind,
        "symbol": symbol,
        "level": level,
    }


def pct_move_alert(symbol: str, reference: float, pct: float) -> dict:
    """Alert when price moves ``pct`` percent from ``reference``.

    ``pct`` > 0 watches an upward move, ``pct`` < 0 a downward move.
    """
    if reference <= 0:
        raise ValueError("reference must be positive")
    if pct == 0:
        raise ValueError("pct must be non-zero")
    level = reference * (1.0 + pct / 100.0)
    kind = "above" if pct > 0 else "below"
    return {
        "id": _alert_id(f"pct_{pct:+g}", f"{level:.2f}"),
        "kind": kind,
        "symbol": symbol,
        "level": level,
        "reference": reference,
        "pct": pct,
    }


def time_alert(symbol: str, hhmm: str, label: str = "") -> dict:
    """An alert that fires when the IST clock reaches ``hhmm``."""
    parse_hhmm(hhmm)
    return {
        "id": _alert_id("time", hhmm),
        "kind": "time",
        "symbol": symbol,
        "time": hhmm,
        "label": label,
    }


def check_alerts(
    alerts: list[dict],
    current_price: float,
    now_ist: datetime,
    fired: set[str] | None = None,
) -> list[dict]:
    """Return the alerts newly triggered by this check.

    Args:
        alerts: Alert dicts from ``build_alerts`` / the helpers.
        current_price: Latest price (supplied by the caller).
        now_ist: Current time, timezone-aware in Asia/Kolkata.
        fired: Set of alert ids that already rang. Newly triggered ids
            are added to it, so each alert rings exactly once. A fresh
            empty set is created when None is passed.

    Firing rules:
        * stop — long: price <= stop; short: price >= stop.
        * target — long: price >= target; short: price <= target.
        * above — price >= level; below — price <= level.
        * flat_by / time — now >= the alert time (IST).
    """
    if fired is None:
        fired = set()
    if now_ist.tzinfo is None:
        raise ValueError("now_ist must be timezone-aware (Asia/Kolkata)")
    now_ist = now_ist.astimezone(IST)
    now_time = now_ist.time()

    triggered: list[dict] = []
    for alert in alerts:
        alert_id = alert["id"]
        if alert_id in fired:
            continue
        kind = alert["kind"]
        hit = False
        if kind == "stop":
            hit = (
                current_price <= alert["level"]
                if alert.get("direction", "long") == "long"
                else current_price >= alert["level"]
            )
        elif kind == "target":
            hit = (
                current_price >= alert["level"]
                if alert.get("direction", "long") == "long"
                else current_price <= alert["level"]
            )
        elif kind == "above":
            hit = current_price >= alert["level"]
        elif kind == "below":
            hit = current_price <= alert["level"]
        elif kind in ("flat_by", "time"):
            hit = now_time >= parse_hhmm(alert["time"])
        if hit:
            fired.add(alert_id)
            triggered.append(alert)
    return triggered


def _demo() -> None:
    """Offline demo on synthetic prices — no network, no live quotes."""
    plan = AlertPlan(
        symbol="NIFTY", direction="long", stop=25_700.0, target=25_900.0,
        entry=25_800.0,
    )
    alerts = build_alerts(plan)
    alerts.append(price_alert("NIFTY", "above", 25_850.0))
    alerts.append(time_alert("NIFTY", "15:00", label="last 15 min warning"))
    fired: set[str] = set()

    # Synthetic ticks: (time IST, price).
    ticks = [
        ("10:00", 25_820.0),
        ("11:30", 25_855.0),  # above 25,850 fires
        ("13:00", 25_905.0),  # target fires
        ("14:00", 25_690.0),  # stop fires (already-fired target not repeated)
        ("15:00", 25_690.0),  # 15:00 time alert fires
        ("15:15", 25_690.0),  # flat-by fires
        ("15:20", 25_600.0),  # nothing new — every alert already rang once
    ]
    for hhmm, price in ticks:
        hour, minute = (int(p) for p in hhmm.split(":"))
        now = datetime(2026, 11, 26, hour, minute, tzinfo=IST)
        new = check_alerts(alerts, price, now, fired)
        for alert in new:
            detail = alert.get("level", alert.get("time"))
            print(f"{hhmm} price={price:,.2f}  FIRED {alert['kind']} "
                  f"({alert['symbol']}) at {detail}")
        if not new:
            print(f"{hhmm} price={price:,.2f}  (no new alerts)")
    print(f"fired ids: {sorted(fired)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Alerts demo (offline, synthetic).")
    parser.parse_args()
    _demo()
