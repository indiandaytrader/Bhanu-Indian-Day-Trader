"""CLI position-sizing calculator for NSE day trading.

Usage:
    python -m bhanu.sizing --entry 226.55 --stop 176.55 \\
        --budget 100000 --max-loss 500

Prints risk per unit, quantity (the four-limit minimum), position value,
max loss at the stop, R:R, and PASS/REFUSE with a reason.  Refuses clearly
when the stop is too far away for the max loss — the stop is never shrunk
to fit more size.

Sizing comes from :func:`bhanu.risk.size_position`; limits come from
:func:`bhanu.config.load_config` when ``bhanu/config.py`` exists, otherwise
from :func:`bhanu.risk.default_config` with the CLI values applied.

Paper trading only: nothing here places an order.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import asdict, fields as dc_fields
from typing import Optional

from .risk import RiskConfig, default_config, size_position


def load_limits(args: argparse.Namespace) -> RiskConfig:
    """Build a :class:`RiskConfig` from ``bhanu.config`` if it exists,
    otherwise from the built-in defaults, then apply CLI overrides.

    ``bhanu.config.load_config`` returns its own ``Config`` dataclass, so
    only the fields :class:`RiskConfig` knows about are carried across
    (e.g. ``budget``, ``max_loss_per_trade``, ``min_rr``, ``target_rr``)."""
    cfg: Optional[RiskConfig] = None
    try:
        from .config import load_config  # type: ignore[import-not-found]

        loaded = load_config()
        if isinstance(loaded, RiskConfig):
            cfg = loaded
        else:
            try:
                overrides = asdict(loaded)
            except TypeError:
                overrides = dict(loaded)
            known = {f.name for f in dc_fields(RiskConfig)}
            cfg = default_config(**{k: v for k, v in overrides.items() if k in known})
    except (ImportError, AttributeError, TypeError):
        cfg = None
    if cfg is None:
        cfg = default_config()

    cfg.budget = args.budget
    cfg.max_loss_per_trade = args.max_loss
    if args.max_order is not None:
        cfg.max_order = args.max_order
    if args.position_cap_pct is not None:
        cfg.position_cap_pct = args.position_cap_pct
    if args.cash_buffer_pct is not None:
        cfg.cash_buffer_pct = args.cash_buffer_pct
    if args.min_rr is not None:
        cfg.min_rr = args.min_rr
    if args.target_rr is not None:
        cfg.target_rr = args.target_rr
    return cfg


def size_report(
    entry: float,
    stop: float,
    config: RiskConfig,
    target: Optional[float] = None,
) -> dict:
    """Size one trade and return the figures the CLI prints."""
    risk_per_unit = abs(entry - stop)
    qty = size_position(entry, stop, config, target=target)
    position_value = qty * entry
    max_loss = qty * risk_per_unit
    if target is not None:
        rr = abs(target - entry) / risk_per_unit if risk_per_unit > 0 else 0.0
    else:
        rr = config.target_rr

    if risk_per_unit <= 0:
        verdict, reason = "REFUSE", "stop equals entry — risk per unit is zero"
    elif qty == 0:
        if config.max_loss_per_trade / risk_per_unit < 1:
            verdict, reason = (
                "REFUSE",
                f"stop is too far for the max loss: risk per unit ₹{risk_per_unit:.2f} "
                f"already exceeds max loss/trade ₹{config.max_loss_per_trade:,.2f}, "
                f"so not even 1 unit fits",
            )
        elif rr < config.min_rr:
            verdict, reason = (
                "REFUSE",
                f"R:R {rr:.2f} is below the minimum {config.min_rr:.2f}",
            )
        else:
            verdict, reason = (
                "REFUSE",
                "four-limit sizing gives 0 units "
                "(position cap / max order / cash buffer too small for 1 unit)",
            )
    else:
        verdict, reason = "PASS", "sized within all four limits"
    return {
        "entry": entry,
        "stop": stop,
        "target": target,
        "risk_per_unit": risk_per_unit,
        "qty": qty,
        "position_value": position_value,
        "max_loss": max_loss,
        "rr": rr,
        "verdict": verdict,
        "reason": reason,
    }


def format_report(report: dict, config: RiskConfig) -> str:
    """Plain-text report, one figure per line."""
    lines = [
        f"entry:            ₹{report['entry']:,.2f}",
        f"stop:             ₹{report['stop']:,.2f}",
    ]
    if report["target"] is not None:
        lines.append(f"target:           ₹{report['target']:,.2f}")
    lines += [
        f"risk per unit:    ₹{report['risk_per_unit']:,.2f}",
        f"qty:              {report['qty']} unit(s)",
        f"position value:   ₹{report['position_value']:,.2f}",
        f"max loss at stop: ₹{report['max_loss']:,.2f}",
        f"R:R:              {report['rr']:.2f}",
        f"limits:           budget ₹{config.budget:,.0f}, "
        f"max loss/trade ₹{config.max_loss_per_trade:,.2f}, "
        f"position cap {config.position_cap_pct:.0%}, "
        f"max order ₹{config.max_order:,.2f}, "
        f"cash buffer {config.cash_buffer_pct:.0%}",
        f"{report['verdict']}: {report['reason']}",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Position-sizing calculator (paper trading; no orders placed)."
    )
    parser.add_argument("--entry", type=float, required=True, help="entry price (₹)")
    parser.add_argument("--stop", type=float, required=True, help="stop price (₹)")
    parser.add_argument("--target", type=float, default=None, help="target price (₹)")
    parser.add_argument("--budget", type=float, default=100_000.0, help="account size (₹)")
    parser.add_argument("--max-loss", type=float, default=500.0,
                        help="max loss per trade (₹)")
    parser.add_argument("--max-order", type=float, default=None,
                        help="max value of one order (₹)")
    parser.add_argument("--position-cap-pct", type=float, default=None,
                        help="max fraction of budget in one position (e.g. 0.75)")
    parser.add_argument("--cash-buffer-pct", type=float, default=None,
                        help="fraction of budget kept in cash (e.g. 0.10)")
    parser.add_argument("--min-rr", type=float, default=None,
                        help="minimum R:R to take the trade")
    parser.add_argument("--target-rr", type=float, default=None,
                        help="planned R:R when no --target is given")
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.entry <= 0 or args.stop <= 0:
        print("REFUSE: entry and stop must both be > 0", file=sys.stderr)
        return 2
    config = load_limits(args)
    report = size_report(args.entry, args.stop, config, target=args.target)
    print(format_report(report, config))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    # No arguments: offline demo on synthetic numbers (not real prices).
    # With arguments: run the calculator.
    if len(sys.argv) == 1:
        demo_cfg = default_config(budget=100_000.0, max_loss_per_trade=500.0)
        print(format_report(size_report(226.55, 176.55, demo_cfg), demo_cfg))
        print("-" * 60)
        # Stop too far for the max loss: risk ₹200/unit vs ₹150 max loss
        # fits 0 units -> REFUSE.
        tight_cfg = default_config(budget=100_000.0, max_loss_per_trade=150.0)
        print(format_report(size_report(226.55, 26.55, tight_cfg), tight_cfg))
        print("-" * 60)
    else:
        raise SystemExit(main())
