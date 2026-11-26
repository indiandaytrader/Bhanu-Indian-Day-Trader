"""All 11 DayTrader skills — one overview with live status.

The dashboard shows some skills on their own pages (scanner on /orb,
news on home, sizing on /sizing, paper on /paper, options on /chain),
but the rest (timemachine, alerts, analyst, risk gate, entry/exit)
lived only on the CLI. This module collects every skill's LIVE status
in one place so the /skills dashboard page can show all 11 together.

Honesty rules (same as the rest of the repo):
* A skill whose live data cannot be fetched says so plainly —
  "CLI only" or "unavailable (reason)" — and never shows fake data.
* trader-budget was folded into the sizing calculator (Bhanu's call,
  26 Nov 2026) and trader-pelosi was skipped on purpose (US Congress
  disclosures only, no Indian equivalent). Both are listed here so the
  user can see they were considered, not missed.
* The Monte Carlo summary runs a SMALL replay (n=200) on the real
  5m bars; it is a reality check, not proof of an edge.

Paper trading only: nothing here places an order.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bhanu import alerts as alerts_mod
from bhanu import analyst as analyst_mod
from bhanu import data as data_mod
from bhanu import levels as levels_mod
from bhanu import news as news_mod
from bhanu import paper as paper_mod
from bhanu import risk as risk_mod
from bhanu import sizing as sizing_mod
from bhanu import timemachine as tm_mod

IST = ZoneInfo("Asia/Kolkata")
REPO_ROOT = Path(__file__).resolve().parent.parent
WATCHLIST_PATH = REPO_ROOT / "data" / "watchlist.json"

#: The 11 DayTrader skills, in the repo's own order. Each entry names
#: the DayTrader skill, our module, what it does, and where it shows
#: on the dashboard (or "CLI only").
SKILL_DEFS: list[dict[str, str]] = [
    {
        "daytrader": "trader-scanner",
        "module": "bhanu/scanner.py",
        "does": "Daily setups + intraday ORB scan (09:15-09:30 IST range, close-confirmation).",
        "shows": "/orb",
    },
    {
        "daytrader": "trader-news",
        "module": "bhanu/news.py",
        "does": "NSE/BSE exchange announcements + Yahoo India headlines; flags market-moving news.",
        "shows": "home (small)",
    },
    {
        "daytrader": "trader-entry",
        "module": "bhanu/entry.py",
        "does": "ORB entry plan: first completed 5m bar closing outside the 09:15-09:30 range.",
        "shows": "/levels",
    },
    {
        "daytrader": "trader-exit",
        "module": "bhanu/exit.py",
        "does": "Stop + target set before entry: range midpoint / 1.5x ATR stop, 2R target, flat 15:15.",
        "shows": "/levels",
    },
    {
        "daytrader": "trader-timemachine",
        "module": "bhanu/timemachine.py",
        "does": "Monte Carlo (10,000 replays) + history replay of a trade — a reality check, not an edge.",
        "shows": "/skills (summary)",
    },
    {
        "daytrader": "trader-budget",
        "module": "bhanu/sizing.py",
        "does": "Four-limit sizing math. FOLDED into the sizing calculator — no standalone skill (Bhanu's call).",
        "shows": "/sizing",
    },
    {
        "daytrader": "trader-risk",
        "module": "bhanu/risk.py",
        "does": "PASS/BLOCK risk gate: fresh-price re-measure, daily-loss accounting, exits never blocked.",
        "shows": "/skills",
    },
    {
        "daytrader": "trader-broker",
        "module": "bhanu/paper.py",
        "does": "Execution. Alpaca/IBKR are US-only, so this is the paper-trading ledger — no live broker, no real orders.",
        "shows": "/paper",
    },
    {
        "daytrader": "trader-alerts",
        "module": "bhanu/alerts.py",
        "does": "Stop / target / flat-by alerts for an open trade — each rings once.",
        "shows": "/skills",
    },
    {
        "daytrader": "trader-analyst",
        "module": "bhanu/analyst.py",
        "does": "NSE filings/results instead of SEC XBRL. The feed lists announcements, not figures — availability only, never invented numbers.",
        "shows": "/skills",
    },
    {
        "daytrader": "trader-pelosi",
        "module": "—",
        "does": "SKIPPED on purpose — US Congress disclosures only, no Indian equivalent, ~zero day-trading value (disclosures are weeks old).",
        "shows": "—",
    },
]

_MC_REPLAYS = 200  # small summary for the dashboard (CLI default is 10,000)


def _orb_watchlist() -> dict[str, Any] | None:
    """Today's ORB scan from data/watchlist.json when it is fresh (<5 min)."""
    if not WATCHLIST_PATH.exists():
        return None
    if time.time() - WATCHLIST_PATH.stat().st_mtime >= 300:
        return None
    try:
        return json.loads(WATCHLIST_PATH.read_text())
    except Exception:  # noqa: BLE001
        return None


def _nifty_plan() -> dict[str, Any]:
    """Today's NIFTY ORB plan (entry/stop/target) via the levels module.

    Returns {} when there is no plan yet (no trigger) or the fetch fails.
    """
    try:
        row = levels_mod.compute_levels("NIFTY")
    except Exception:  # noqa: BLE001
        return {}
    if not row.get("signal"):
        return {"signal": None}
    return {
        "signal": row["signal"],
        "entry": row["entry"],
        "stop": row["stop"],
        "atr_stop": row["atr_stop"],
        "target": row["target"],
        "price": row["price"],
        "range_high": row["orb_high"],
        "range_low": row["orb_low"],
    }


def _timemachine_summary(plan: dict[str, Any]) -> str:
    """Small Monte Carlo summary for today's NIFTY plan, or an honest reason."""
    if not plan.get("entry") or not plan.get("stop") or not plan.get("target"):
        return "No ORB plan today — nothing to replay (CLI: python -m bhanu.timemachine --help)."
    try:
        bars = data_mod.fetch_intraday_bars("^NSEI", interval="5m", days=60)
    except Exception as exc:  # noqa: BLE001
        return f"Unavailable — could not fetch 60 days of 5m bars: {exc}"
    if bars.empty:
        return "Unavailable — no 5m bars for the replay pool."
    mc_bars = [
        {
            "time": ts.to_pydatetime(),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
        }
        for ts, r in bars.iterrows()
    ]
    try:
        mc = tm_mod.monte_carlo(
            mc_bars,
            entry=float(plan["entry"]),
            stop=float(plan["stop"]),
            target=float(plan["target"]),
            direction=str(plan["signal"]),
            n=_MC_REPLAYS,
        )
    except ValueError as exc:
        return f"CLI only — {exc} (run python -m bhanu.timemachine for the full 10,000-replay check)."
    v = tm_mod.verdict(mc["target_first_pct"], mc["stop_first_pct"], mc["reward_risk"])
    return (
        f"{_MC_REPLAYS} replays on NIFTY 5m bars: target first "
        f"{mc['target_first_pct']:.0f}%, stop first {mc['stop_first_pct']:.0f}%, "
        f"neither {mc['neither_pct']:.0f}%, avg {mc['avg_r']:+.2f}R — "
        f"verdict: {v['verdict']} (break-even {v['break_even_pct']:.0f}%). "
        f"Reality check only, not an edge."
    )


def _risk_gate_status(plan: dict[str, Any]) -> str:
    """Today's risk gate verdict on the NIFTY ORB plan (or why none exists)."""
    if not plan.get("entry") or not plan.get("stop"):
        return "No plan to gate — no ORB trigger yet today."
    try:
        cfg = risk_mod.default_config()
        qty = risk_mod.size_position(float(plan["entry"]), float(plan["stop"]), cfg)
        order = {
            "symbol": "NIFTY",
            "side": "sell" if str(plan["signal"]) == "short" else "buy",
            "direction": str(plan["signal"]),
            "qty": qty,
            "entry": float(plan["entry"]),
            "stop": float(plan["stop"]),
            "target": float(plan["target"]),
            "asset": "index",
            "time": datetime.now(IST).time(),
        }
        day_state = {"entries_today": [], "trades_today": 0}
        verdict, reason = risk_mod.gate(order, cfg, day_state)
        detail = f"qty {qty} @ {plan['entry']:,.2f}, stop {plan['stop']:,.2f}"
        if reason and reason != "ok":
            detail += f" — {reason}"
        return f"{verdict} — {detail}"
    except Exception as exc:  # noqa: BLE001
        return f"Unavailable — {exc}"


def _alerts_status(plan: dict[str, Any]) -> str:
    """Alerts armed/fired for today's NIFTY plan, or why there are none."""
    if not plan.get("entry") or not plan.get("stop") or not plan.get("target"):
        return "No open plan — no alerts armed (alerts are built from a trade plan before entry)."
    try:
        alert_plan = alerts_mod.AlertPlan(
            symbol="NIFTY",
            direction=str(plan["signal"]),
            stop=float(plan["stop"]),
            target=float(plan["target"]),
            entry=float(plan["entry"]),
        )
        built = alerts_mod.build_alerts(alert_plan)
        price = float(plan["price"])
        fired = alerts_mod.check_alerts(built, price, datetime.now(IST), set())
        armed = [a["kind"] for a in built]
        if fired:
            return f"{len(fired)} fired at price {price:,.2f}: " + ", ".join(
                a["kind"] for a in fired
            )
        return (
            f"3 armed (stop {plan['stop']:,.2f} / target {plan['target']:,.2f} / "
            f"flat-by 15:15), none fired yet at price {price:,.2f} — each rings once."
        )
    except Exception as exc:  # noqa: BLE001
        return f"Unavailable — {exc}"


def _analyst_status() -> str:
    """NSE filings availability for RELIANCE (honest: feed has no figures)."""
    try:
        result = analyst_mod.latest_results("RELIANCE")
    except Exception as exc:  # noqa: BLE001
        return f"Unavailable — {exc}"
    if result.get("available"):
        return "Latest results figures available (see analyst module)."
    reason = str(result.get("reason", "figures not available"))
    announced = result.get("announcement_date", "")
    extra = f" Latest results announcement: {announced}." if announced else ""
    return f"Figures NOT in the feed — {reason}.{extra}"


def collect_skill_status(live: bool = True) -> list[dict[str, str]]:
    """One status row per DayTrader skill (11 rows, always).

    With ``live=True`` each row carries a live status computed now from
    the repo's own modules (fetches are best-effort; a failure is
    reported honestly, never faked). With ``live=False`` no network is
    touched — rows say where the skill lives and that its live status
    needs a live run (used by the offline tests).
    """
    plan: dict[str, Any] = {}
    watchlist: dict[str, Any] | None = None
    news: dict[str, Any] | None = None
    paper_summary: dict[str, Any] | None = None
    if live:
        plan = _nifty_plan()
        watchlist = _orb_watchlist()
        try:
            news = news_mod.check_symbol("NIFTY")
        except Exception:  # noqa: BLE001
            news = None
        try:
            paper_summary = paper_mod.day_summary(datetime.now(IST).date())
        except Exception:  # noqa: BLE001
            paper_summary = None

    def _scanner_status() -> str:
        if not live:
            return "Live status needs a live run — see /orb."
        if watchlist is None:
            return "No fresh scan — open /orb to run the ORB scan."
        hits = watchlist.get("hits", [])
        top = ", ".join(
            f"{h.get('symbol')} {h.get('direction')}" for h in hits[:5]
        )
        return (
            f"Today: {len(hits)} ORB hit(s)"
            + (f" — top: {top}." if top else ".")
            + f" Usable {watchlist.get('usable')}/{watchlist.get('fetched')} symbols."
        )

    def _news_status() -> str:
        if not live:
            return "Live status needs a live run — see home."
        if news is None:
            return "Unavailable — news check failed."
        items = news.get("items", []) or []
        latest = news.get("latest", []) or []
        sources = news.get("sources", {}) or {}
        moving = news.get("moving")
        parts: list[str] = []
        if items:
            fresh = "; ".join(
                f"{it.get('source', '')}: {it.get('title', '')}" for it in items[:3]
            )
            parts.append(f"Fresh ({len(items)}): {fresh}")
        if latest:
            heads = "; ".join(it.get("title", "") for it in latest[:3])
            parts.append(f"Latest headlines: {heads}")
        nse_note = sources.get("nse_announcements", "")
        yahoo_note = sources.get("yahoo_headlines", "")
        parts.append(f"NSE announcements: {nse_note}; Yahoo headlines: {yahoo_note}")
        parts.append(f"Moving (>=3%): {moving}")
        if not items and not latest:
            return (
                "No fresh items and no headlines returned right now "
                f"({'; '.join(parts)}). News is a filter, never an entry signal alone."
            )
        return " | ".join(parts)

    def _entry_status() -> str:
        if not live:
            return "Live status needs a live run — see /levels."
        if not plan.get("signal"):
            return "No ORB trigger yet today (09:15-09:30 range, close-confirmation)."
        return (
            f"NIFTY {plan['signal']} entry {plan['entry']:,.2f} "
            f"(range {plan['range_low']:,.2f}-{plan['range_high']:,.2f})."
        )

    def _exit_status() -> str:
        if not live:
            return "Live status needs a live run — see /levels."
        if not plan.get("signal"):
            return "No plan today — no stop/target set."
        atr = f", 1.5x ATR stop {plan['atr_stop']:,.2f}" if plan.get("atr_stop") else ""
        return (
            f"NIFTY {plan['signal']}: stop {plan['stop']:,.2f}{atr}, "
            f"target {plan['target']:,.2f} (2R), flat by 15:15."
        )

    def _sizing_status() -> str:
        if not live:
            return "Folded into sizing — budget is an input (see /sizing)."
        cfg = sizing_mod.default_config()
        report = sizing_mod.size_report(226.55, 176.55, cfg)
        return (
            f"Folded into sizing — budget is an input, not a skill. "
            f"Example (entry 226.55 / stop 176.55): qty {report['qty']}, "
            f"verdict {report['verdict']} (see /sizing)."
        )

    def _paper_status() -> str:
        if not live:
            return "Live status needs a live run — see /paper."
        if paper_summary is None:
            return "Unavailable — paper ledger could not be read."
        return (
            f"Today: {paper_summary.get('trades', 0)} trade(s), realised "
            f"₹{paper_summary.get('realised_pnl', 0):,.2f}, "
            f"flat: {paper_summary.get('flat')} (see /paper)."
        )

    status_fns = {
        "trader-scanner": _scanner_status,
        "trader-news": _news_status,
        "trader-entry": _entry_status,
        "trader-exit": _exit_status,
        "trader-timemachine": lambda: (
            _timemachine_summary(plan) if live else "Live status needs a live run (CLI: python -m bhanu.timemachine)."
        ),
        "trader-budget": _sizing_status,
        "trader-risk": lambda: (
            _risk_gate_status(plan) if live else "Live status needs a live run."
        ),
        "trader-broker": _paper_status,
        "trader-alerts": lambda: (
            _alerts_status(plan) if live else "Live status needs a live run."
        ),
        "trader-analyst": lambda: (
            _analyst_status() if live else "Live status needs a live run."
        ),
        "trader-pelosi": lambda: (
            "Skipped — US Congress disclosures only, no Indian equivalent."
        ),
    }
    rows: list[dict[str, str]] = []
    for skill in SKILL_DEFS:
        name = skill["daytrader"]
        fn = status_fns.get(name)
        try:
            status = fn() if fn is not None else "Unavailable."
        except Exception as exc:  # noqa: BLE001
            status = f"Unavailable — {exc}"
        rows.append(
            {
                "daytrader": name,
                "module": skill["module"],
                "does": skill["does"],
                "shows": skill["shows"],
                "status": status,
            }
        )
    return rows


def main() -> int:
    print("11 DayTrader skills — live status (26 Nov 2026 run):")
    for row in collect_skill_status():
        print(f"  {row['daytrader']} ({row['module']}) — {row['shows']}")
        print(f"    {row['does']}")
        print(f"    LIVE: {row['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
