"""Time Machine page data: Monte Carlo, trade check, ORB backtest.

This module collects, for the dashboard's /timemachine page, the three
things DayTrader does on the backend (CLI only until now):

1. **Monte Carlo Time Machine** — today's best ORB setup replayed
   10,000 times on the symbol's own last 60 days of 5-minute bars
   (``bhanu.timemachine``). Win rate, avg R (gross & net of costs),
   best case / worst case, limitations, and brokerage/slippage
   scenarios. A reality check, not proof of an edge.
2. **Trade check** — the same setup through the risk gate
   (PASS/BLOCK), the four-limit sizing, how many such positions the
   budget allows, SL/TP and R:R.
3. **ORB backtest** — the honest backtest from
   ``scripts/backtest_orb_nifty.py`` (history replay on real NIFTY 5m
   bars): fires, wins, losses, avg R. No accuracy percentage is ever
   claimed.

Honesty: every section is best-effort. When live data is missing or
there are too few full sessions, the section says "unavailable" with
the reason — no fake numbers are ever shown. Paper trading only:
nothing here places an order.
"""

from __future__ import annotations

import importlib.util
import math
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bhanu import data as data_mod
from bhanu import levels as levels_mod
from bhanu import risk as risk_mod
from bhanu import sizing as sizing_mod
from bhanu import timemachine as tm_mod

IST = ZoneInfo("Asia/Kolkata")
REPO_ROOT = Path(__file__).resolve().parent.parent

MC_REPLAYS = 10_000  # DayTrader default for the Time Machine
BACKTEST_RANGE = "1mo"

#: Default cost assumptions passed to ``timemachine.monte_carlo`` for the
#: net-of-costs histogram (``histogram_net`` / ``avg_r_net``):
#:   - ``DEFAULT_BROKERAGE_PER_ORDER``: ₹20/order (standard Indian discount
#:     broker flat fee; ₹0 in ``timemachine.py`` raw default).
#:   - ``DEFAULT_SLIPPAGE_BPS``: 5 bps/side (0.05% per side; 0 bps in
#:     ``timemachine.py`` raw default).
DEFAULT_BROKERAGE_PER_ORDER: float = 20.0
DEFAULT_SLIPPAGE_BPS: float = 5.0

#: Scenario grids rendered in the Charges & Slippage table on /timemachine:
#: brokerage in ₹/order and slippage in bps/side (including the 0 / 0
#: zero-cost baseline from ``timemachine.py``).
BROKERAGE_SCENARIOS: list[float] = [0.0, 20.0, 40.0]
SLIPPAGE_SCENARIOS: list[float] = [0.0, 5.0, 10.0]

#: Real methodological limitations of the Monte Carlo Time Machine
#: (mirrors ``bhanu/timemachine.py`` module docstring).
MC_LIMITATIONS: list[str] = [
    "Resampled 5-minute bars destroy autocorrelation, volatility clustering, and intraday trend persistence.",
    "Assumes the next session behaves like the last 60 days of full 75-bar NSE sessions (regime shifts are not modelled).",
    "A gap through the stop-loss is scored as exactly -1R even though a real fill can suffer worse slippage.",
    "Gross outcomes ignore brokerage, STT, exchange fees, GST, and bid-ask slippage — check the Net R distribution and Charges & Slippage scenarios.",
    "For options trades, spot/underlying bar replays ignore theta decay and IV crush; run replays on the option premium series instead.",
    "Reality check only, not proof of an edge — never trade on Monte Carlo odds alone.",
]


# ---------------------------------------------------------------------------
# Today's best ORB setup
# ---------------------------------------------------------------------------

def best_orb_setup() -> dict[str, Any]:
    """Today's best ORB setup (NIFTY first, else the first index with one).

    Returns the levels row for the chosen index, or {} when no index
    has an ORB signal today. The levels row already carries entry,
    stop, atr_stop, target, price and the ORB range.
    """
    try:
        result = levels_mod.scan_levels()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"levels unavailable: {exc}"}
    rows = result.get("levels", [])
    if not rows:
        return {"error": "no index levels computed today"}
    # Prefer NIFTY, then the first row with a signal.
    for row in rows:
        if row.get("symbol") == "NIFTY" and row.get("signal"):
            return dict(row)
    for row in rows:
        if row.get("signal"):
            return dict(row)
    return {"error": "no ORB signal on any index today"}


# ---------------------------------------------------------------------------
# Monte Carlo
# ---------------------------------------------------------------------------

def _bars_to_mc(bars) -> list[dict]:
    """DataFrame of 5m bars -> timemachine Bar dicts (skips NaN rows)."""
    out: list[dict] = []
    for ts, row in bars.iterrows():
        if row.isna().any():
            continue
        out.append(
            {
                "time": ts.to_pydatetime(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            }
        )
    return out


def monte_carlo_summary(
    setup: dict[str, Any],
    brokerage_per_order: float = DEFAULT_BROKERAGE_PER_ORDER,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
) -> dict[str, Any]:
    """10,000-replay Monte Carlo for ``setup`` (a levels row).

    Returns the MC figures (gross and net of default costs), verdict,
    best/worst case in R, ``mc_limitations``, ``histogram``,
    ``histogram_net``, ``brokerage_scenarios``, and
    ``slippage_scenarios``. On any failure returns
    ``{"available": False, "reason": ...}`` — never fake numbers.
    """
    entry = setup.get("entry")
    stop = setup.get("stop")
    target = setup.get("target")
    direction = setup.get("signal")
    symbol = setup.get("symbol", "NIFTY")
    if not entry or not stop or not target or not direction:
        return {
            "available": False,
            "reason": "no ORB setup to replay (no entry/stop/target today)",
        }
    yahoo_symbol = data_mod.UNIVERSE.get(symbol, "^NSEI")
    try:
        bars = data_mod.fetch_intraday_bars(yahoo_symbol, interval="5m", days=60)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"could not fetch 60 days of 5m bars: {exc}"}
    mc_bars = _bars_to_mc(bars)
    if not mc_bars:
        return {"available": False, "reason": "no 5m bars for the replay pool"}
    try:
        mc = tm_mod.monte_carlo(
            mc_bars,
            entry=float(entry),
            stop=float(stop),
            target=float(target),
            direction=str(direction),
            n=MC_REPLAYS,
            collect_outcomes=True,
            brokerage_per_order=float(brokerage_per_order),
            slippage_bps=float(slippage_bps),
        )
    except ValueError as exc:
        return {"available": False, "reason": str(exc)}
    v = tm_mod.verdict(mc["target_first_pct"], mc["stop_first_pct"], mc["reward_risk"])
    entry_f = float(entry)
    stop_f = float(stop)
    target_f = float(target)
    risk = abs(entry_f - stop_f)
    reward = abs(target_f - entry_f)

    # Gross and net distributions of the 10,000 replay outcomes (R buckets).
    from bhanu import charts as charts_mod

    histogram = charts_mod.mc_histogram(mc.get("outcomes", []), mc["avg_r"])
    avg_r_net = mc.get("avg_r_net", mc["avg_r"])
    histogram_net = charts_mod.mc_histogram(
        mc.get("outcomes_net", mc.get("outcomes", [])), avg_r_net
    )

    # Per-scenario cost table (also consumable alongside the raw scenario grids).
    cost_scenarios: list[dict[str, float]] = []
    for br in BROKERAGE_SCENARIOS:
        for sl in SLIPPAGE_SCENARIOS:
            slip_factor = sl / 10000.0
            cost_rs = 2.0 * br + 2.0 * entry_f * slip_factor
            cost_r = cost_rs / risk if risk else 0.0
            cost_scenarios.append(
                {
                    "brokerage_per_order": br,
                    "slippage_bps": sl,
                    "cost_per_trade_rs": round(cost_rs, 2),
                    "cost_in_r": round(cost_r, 4),
                    "avg_r_gross": round(mc["avg_r"], 4),
                    "avg_r_net": round(mc["avg_r"] - cost_r, 4),
                }
            )

    return {
        "available": True,
        "symbol": symbol,
        "direction": direction,
        "entry": entry_f,
        "stop": stop_f,
        "target": target_f,
        "n": mc["n"],
        "target_first_pct": mc["target_first_pct"],
        "stop_first_pct": mc["stop_first_pct"],
        "neither_pct": mc["neither_pct"],
        "avg_r": mc["avg_r"],
        "avg_r_net": avg_r_net,
        "brokerage_per_order": float(brokerage_per_order),
        "slippage_bps": float(slippage_bps),
        "reward_risk": mc["reward_risk"],
        # Best case = target hit first (+R), worst case = stop hit first (-1R).
        "best_case_r": reward / risk if risk else 0.0,
        "worst_case_r": -1.0,
        "verdict": v["verdict"],
        "edge_pts": v["edge_pts"],
        "break_even_pct": v["break_even_pct"],
        "histogram": histogram,
        "histogram_net": histogram_net,
        "mc_limitations": list(MC_LIMITATIONS),
        "brokerage_scenarios": list(BROKERAGE_SCENARIOS),
        "slippage_scenarios": list(SLIPPAGE_SCENARIOS),
        "cost_scenarios": cost_scenarios,
    }


# ---------------------------------------------------------------------------
# User's own trade check (Time Machine form)
# ---------------------------------------------------------------------------

#: Direction aliases accepted by the form: display CALL/PUT map to the
#: internal 'long'/'short' — internal values never change anywhere.
_DIRECTION_ALIASES = {
    "long": "long",
    "call": "long",
    "short": "short",
    "put": "short",
}


def check_trade(symbol, entry, stop, target, direction) -> dict[str, Any]:
    """Run the user's own trade through the same 10,000-replay Time
    Machine as the auto setup (``monte_carlo_summary``) plus the same
    trade check (``trade_check``).

    Read-only and isolated: it never touches the robot, signals, scans
    or the poller. Returns ``{"ok": True, ...}`` on success or
    ``{"ok": False, "error": ...}`` on any problem — never raises for
    bad input.
    """
    raw_symbol = "" if symbol is None else str(symbol).strip().upper()
    if not raw_symbol or raw_symbol not in data_mod.UNIVERSE:
        return {
            "ok": False,
            "error": f"unknown symbol {raw_symbol or '(blank)'} — "
                     "sirf index ya F&O stock chunna hai (NIFTY, BANKNIFTY, "
                     "SENSEX ya 39 F&O stocks me se)",
        }
    try:
        entry_f = float(entry)
        stop_f = float(stop)
        target_f = float(target)
    except (TypeError, ValueError):
        return {"ok": False, "error": "entry, stop aur target number hone chahiye"}
    if not all(math.isfinite(v) for v in (entry_f, stop_f, target_f)):
        return {"ok": False, "error": "entry, stop aur target number hone chahiye"}
    if entry_f <= 0 or stop_f <= 0 or target_f <= 0:
        return {"ok": False, "error": "entry, stop aur target positive prices hone chahiye"}
    direction_key = "" if direction is None else str(direction).strip().lower()
    if direction_key not in _DIRECTION_ALIASES:
        return {"ok": False, "error": "direction CALL ya PUT hona chahiye"}
    direction = _DIRECTION_ALIASES[direction_key]
    if direction == "long" and not stop_f < entry_f < target_f:
        return {"ok": False, "error": "CALL ke liye stop < entry < target hona chahiye"}
    if direction == "short" and not target_f < entry_f < stop_f:
        return {"ok": False, "error": "PUT ke liye target < entry < stop hona chahiye"}
    setup = {
        "symbol": raw_symbol,
        "signal": direction,
        "entry": entry_f,
        "stop": stop_f,
        "target": target_f,
    }
    mc = monte_carlo_summary(setup)
    if not mc.get("available"):
        return {"ok": False, "error": mc.get("reason", "Monte Carlo unavailable")}
    tc = trade_check(setup)
    return {
        "ok": True,
        "symbol": raw_symbol,
        "direction": direction,
        "entry": entry_f,
        "stop": stop_f,
        "target": target_f,
        "monte_carlo": mc,
        "trade_check": tc,
    }


# ---------------------------------------------------------------------------
# Trade check (risk gate + four-limit sizing + how many positions)
# ---------------------------------------------------------------------------

def trade_check(setup: dict[str, Any]) -> dict[str, Any]:
    """Risk gate + sizing + position count for ``setup`` (a levels row)."""
    entry = setup.get("entry")
    stop = setup.get("stop")
    target = setup.get("target")
    direction = setup.get("signal")
    symbol = setup.get("symbol", "NIFTY")
    if not entry or not stop or not direction:
        return {
            "available": False,
            "reason": "no ORB setup to check (no entry/stop today)",
        }
    cfg = risk_mod.default_config()
    risk_per_unit = abs(float(entry) - float(stop))
    # The four limits (floored to whole units).
    limits = {
        "max_loss": math.floor(cfg.max_loss_per_trade / risk_per_unit) if risk_per_unit else 0,
        "position_cap": math.floor((cfg.budget * cfg.position_cap_pct) / float(entry)),
        "max_order": math.floor(cfg.max_order / float(entry)),
        "cash_buffer": math.floor((cfg.budget * (1.0 - cfg.cash_buffer_pct)) / float(entry)),
    }
    report = sizing_mod.size_report(
        float(entry), float(stop), cfg, target=float(target) if target else None
    )
    qty = int(report["qty"])
    order = {
        "symbol": symbol,
        "side": "sell" if direction == "short" else "buy",
        "direction": direction,
        "qty": qty,
        "entry": float(entry),
        "stop": float(stop),
        "target": float(target) if target else None,
        "asset": "index",
        "time": datetime.now(IST).time(),
    }
    verdict, reason = risk_mod.gate(order, cfg, {"entries_today": [], "trades_today": 0})
    # How many such positions fit: by capital and by the day limit.
    order_value = qty * float(entry)
    cash_available = cfg.budget * (1.0 - cfg.cash_buffer_pct)
    by_capital = math.floor(cash_available / order_value) if order_value > 0 else 0
    by_day = cfg.max_trades_per_day
    return {
        "available": True,
        "symbol": symbol,
        "direction": direction,
        "entry": float(entry),
        "stop": float(stop),
        "target": float(target) if target else None,
        "rr": report["rr"],
        "risk_per_unit": risk_per_unit,
        "qty": qty,
        "position_value": report["position_value"],
        "max_loss": report["max_loss"],
        "sizing_verdict": report["verdict"],
        "sizing_reason": report["reason"],
        "limits": limits,
        "binding_limit": min(limits, key=lambda k: limits[k]) if limits else None,
        "gate_verdict": verdict,
        "gate_reason": reason,
        "positions_by_capital": by_capital,
        "positions_by_day": by_day,
        "positions_max": max(0, min(by_capital, by_day)) if qty > 0 else 0,
        "budget": cfg.budget,
    }


# ---------------------------------------------------------------------------
# ORB backtest (from scripts/backtest_orb_nifty.py)
# ---------------------------------------------------------------------------

def _load_backtest_script():
    """Import scripts/backtest_orb_nifty.py as a module (it is a script)."""
    path = REPO_ROOT / "scripts" / "backtest_orb_nifty.py"
    spec = importlib.util.spec_from_file_location("orb_backtest", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def orb_backtest_summary() -> dict[str, Any]:
    """Honest ORB backtest (history replay on real NIFTY 5m bars).

    Returns per-direction fires/wins/losses/neither/avg R, or
    ``{"available": False, "reason": ...}`` when the run cannot be made
    (e.g. too few full sessions). No accuracy percentage is claimed.
    """
    try:
        mod = _load_backtest_script()
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"backtest script could not be loaded: {exc}"}
    out: dict[str, Any] = {"available": True, "directions": {}}
    for direction in ("long", "short"):
        try:
            r = mod.run(direction, BACKTEST_RANGE)
        except ValueError as exc:
            out["directions"][direction] = {"available": False, "reason": str(exc)}
            continue
        except Exception as exc:  # noqa: BLE001
            out["directions"][direction] = {"available": False, "reason": str(exc)}
            continue
        out["directions"][direction] = {
            "available": True,
            "first_day": r.get("first_day"),
            "last_day": r.get("last_day"),
            "sessions": r.get("sessions"),
            "fires": r.get("fires"),
            "target_wins": r.get("target_wins"),
            "stop_losses": r.get("stop_losses"),
            "neither": r.get("neither"),
            "avg_r": r.get("avg_r"),
        }
    # Range label from whichever direction ran.
    for direction in ("long", "short"):
        d = out["directions"].get(direction, {})
        if d.get("available"):
            out["first_day"] = d["first_day"]
            out["last_day"] = d["last_day"]
            out["sessions"] = d["sessions"]
            break
    return out


def collect_timemachine() -> dict[str, Any]:
    """Everything the /timemachine page shows, in one dict."""
    setup = best_orb_setup()
    if setup.get("error"):
        return {
            "setup": None,
            "setup_error": setup["error"],
            "monte_carlo": {"available": False, "reason": setup["error"]},
            "trade_check": {"available": False, "reason": setup["error"]},
            "backtest": orb_backtest_summary(),
        }
    return {
        "setup": setup,
        "setup_error": None,
        "monte_carlo": monte_carlo_summary(setup),
        "trade_check": trade_check(setup),
        "backtest": orb_backtest_summary(),
    }


def main() -> int:
    data = collect_timemachine()
    if data["setup_error"]:
        print(f"no setup today: {data['setup_error']}")
    else:
        s = data["setup"]
        print(f"best ORB setup: {s['symbol']} {s['signal']} entry {s['entry']:,.2f} "
              f"SL {s['stop']:,.2f} TP {s['target']:,.2f}")
    mc = data["monte_carlo"]
    if mc.get("available"):
        print(f"Monte Carlo ({mc['n']} replays): target first {mc['target_first_pct']:.1f}%, "
              f"stop first {mc['stop_first_pct']:.1f}%, neither {mc['neither_pct']:.1f}%, "
              f"avg {mc['avg_r']:+.3f}R (net {mc['avg_r_net']:+.3f}R), "
              f"best +{mc['best_case_r']:.1f}R, worst {mc['worst_case_r']:.1f}R, "
              f"verdict: {mc['verdict']}")
    else:
        print(f"Monte Carlo unavailable: {mc.get('reason')}")
    tc = data["trade_check"]
    if tc.get("available"):
        print(f"Trade check: gate {tc['gate_verdict']} ({tc['gate_reason']}), "
              f"qty {tc['qty']}, sizing {tc['sizing_verdict']}, "
              f"positions max {tc['positions_max']} "
              f"(capital {tc['positions_by_capital']}, day limit {tc['positions_by_day']})")
    else:
        print(f"Trade check unavailable: {tc.get('reason')}")
    bt = data["backtest"]
    if bt.get("available"):
        for direction, d in bt["directions"].items():
            if d.get("available"):
                print(f"Backtest ORB {direction} ({d['first_day']} to {d['last_day']}, "
                      f"{d['sessions']} sessions): fires={d['fires']} wins={d['target_wins']} "
                      f"losses={d['stop_losses']} neither={d['neither']} avg R={d['avg_r']:+.3f}")
            else:
                print(f"Backtest ORB {direction} unavailable: {d.get('reason')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
