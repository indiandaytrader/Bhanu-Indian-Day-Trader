"""ORB backtest on real NIFTY 5m bars (Yahoo Finance).

Re-runs the ORB rule (09:15-09:30 IST range, close-confirmation trigger,
range-midpoint stop, 2R target, flat by 15:15) on every past full session
and reports honest figures. No accuracy percentage is claimed beyond what
this run measures.

Usage:
    python3 scripts/backtest_orb_nifty.py [--range 1mo] [--direction long|short|both]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bhanu import data as data_mod
from bhanu import timemachine as tm_mod


def fetch_bars_5m(symbol: str = "^NSEI", range_: str = "1mo") -> list[dict]:
    """Fetch 5m bars via the Yahoo chart API and return Bar dicts."""
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?range={range_}&interval=5m"
    )
    payload = data_mod._get_json(url)
    frame = data_mod._chart_to_frame(payload, symbol)
    bars = []
    for ts, row in frame.iterrows():
        if row.isna().any():
            continue
        bars.append(
            {
                "time": ts.to_pydatetime(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            }
        )
    return bars


def orb_rule(direction: str):
    """ORB rule: range = first 3 bars (09:15/09:20/09:25); trigger = first
    later bar that CLOSES above the range high (long) / below the range low
    (short). Stop = range midpoint; target = 2R. Returns None if no trigger."""

    def rule(session):
        if len(session) < 4:
            return None
        rng = session[:3]
        range_high = max(b["high"] for b in rng)
        range_low = min(b["low"] for b in rng)
        midpoint = (range_high + range_low) / 2.0
        for bar in session[3:]:
            if direction == "long" and bar["close"] > range_high:
                entry = bar["close"]
                stop = midpoint
                if entry <= stop:
                    return None
                return {
                    "entry": entry,
                    "stop": stop,
                    "target": entry + 2.0 * (entry - stop),
                }
            if direction == "short" and bar["close"] < range_low:
                entry = bar["close"]
                stop = midpoint
                if entry >= stop:
                    return None
                return {
                    "entry": entry,
                    "stop": stop,
                    "target": entry - 2.0 * (stop - entry),
                }
        return None

    return rule


def run(direction: str, range_: str) -> dict:
    bars = fetch_bars_5m("^NSEI", range_)
    # Dummy levels for the top-level check; the rule returns its own levels.
    entry, stop, target = (25000.0, 24900.0, 25200.0) if direction == "long" else (25000.0, 25100.0, 24800.0)
    result = tm_mod.history_replay(bars, orb_rule(direction), entry, stop, target, direction)
    days = sorted({b["time"].date() for b in bars})
    result["first_day"] = str(days[0]) if days else "?"
    result["last_day"] = str(days[-1]) if days else "?"
    result["direction"] = direction
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ORB backtest on real NIFTY 5m bars.")
    ap.add_argument("--range", default="1mo", help="Yahoo range (e.g. 1mo, 2mo)")
    ap.add_argument("--direction", default="both", choices=["long", "short", "both"])
    args = ap.parse_args(argv)
    directions = ["long", "short"] if args.direction == "both" else [args.direction]
    for direction in directions:
        try:
            r = run(direction, args.range)
        except ValueError as exc:
            print(f"{direction}: cannot run — {exc}")
            continue
        print(
            f"ORB {direction} on NIFTY 5m ({r['first_day']} to {r['last_day']}, "
            f"{r['sessions']} full sessions): "
            f"fires={r['fires']} target_wins={r['target_wins']} "
            f"stop_losses={r['stop_losses']} neither={r['neither']} "
            f"avg_R={r['avg_r']:+.3f}"
        )
        if r["fires"]:
            dec = r["target_wins"] + r["stop_losses"]
            if dec:
                v = tm_mod.verdict(
                    100.0 * r["target_wins"] / dec, 100.0 * r["stop_losses"] / dec, 2.0
                )
                print(f"  verdict: {v['verdict']} (edge {v['edge_pts']:+.1f} pts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
