"""Bhanu Indian Day Trader — web dashboard.

Stdlib-only HTTP server (no framework). Pages:
  /         home — index quotes + paper summary + latest news (NIFTY)
  /orb      ORB scanner (09:15-09:30 IST range, close-confirmation)
  /levels   live support/resistance + ORB entry/SL/TP per index
  /chain    Nifty / Bank Nifty option chain with OI + PCR
  /sizing   position sizing calculator (four limits, budget is an input)
  /paper    paper trading ledger
  /skills   all 11 DayTrader skills — what each does + live status
  /timemachine  Monte Carlo Time Machine + trade check + ORB backtest

Data is fetched live (Yahoo / NSE) with short caching. When a source is
unavailable the page says so plainly — no fake data is ever shown.
Paper trading only: nothing here places a real order.
"""

from __future__ import annotations

import html
import json
import os
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from bhanu import config as config_mod
from bhanu import data as data_mod
from bhanu import i18n as i18n_mod
from bhanu import levels as levels_mod
from bhanu import options as options_mod
from bhanu import paper as paper_mod
from bhanu import scanner as scanner_mod
from bhanu import sizing as sizing_mod
from bhanu import skills as skills_mod
from bhanu import timecheck as timecheck_mod
from bhanu import news as news_mod

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("PORT", "8810"))

_cache: dict[str, tuple[float, object]] = {}


def _cached(key: str, ttl: float, fn):
    now = time.time()
    if key in _cache:
        ts, val = _cache[key]
        if now - ts < ttl:
            return val
    val = fn()
    _cache[key] = (now, val)
    return val


def _lang(query: dict) -> str:
    lang = (query.get("lang") or ["hi"])[0]
    return lang if lang in i18n_mod.LANGUAGES else "hi"


def _esc(value) -> str:
    return html.escape(str(value))


def _page(lang: str, title: str, body: str) -> str:
    t = lambda k: i18n_mod.t(k, lang)
    nav = (
        f'<a href="/?lang={lang}">{t("home")}</a> | '
        f'<a href="/orb?lang={lang}">{t("orb_scanner")}</a> | '
        f'<a href="/levels?lang={lang}">{t("levels")}</a> | '
        f'<a href="/chain?lang={lang}">{t("option_chain")}</a> | '
        f'<a href="/sizing?lang={lang}">{t("sizing")}</a> | '
        f'<a href="/paper?lang={lang}">{t("paper_trading")}</a> | '
        f'<a href="/skills?lang={lang}">{t("skills")}</a> | '
        f'<a href="/timemachine?lang={lang}">{t("timemachine")}</a> | '
        f'<a href="?lang=en">EN</a> <a href="?lang=hi">HI</a>'
    )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>{_esc(title)} — Bhanu Indian Day Trader</title>
<style>body{{font-family:system-ui,sans-serif;max-width:960px;margin:24px auto;padding:0 16px;line-height:1.5}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;padding:6px 8px;text-align:left}}
th{{background:#f0f0f0}} .up{{color:green}} .down{{color:red}} .muted{{color:#666}}
nav{{margin-bottom:16px}}</style></head>
<body><h1>Bhanu Indian Day Trader</h1><nav>{nav}</nav>
<h2>{_esc(title)}</h2>{body}
<p class="muted">Paper trading only — no real orders. Educational software, not investment advice.</p>
</body></html>"""


def _quote_row(symbol: str, label: str) -> str:
    try:
        q = _cached(f"quote:{symbol}", 30, lambda: data_mod.fetch_quote(symbol))
        price = q.get("price")
        prev = q.get("prev_close")
        chg = q.get("change_pct")
        if price is None:
            return f"<tr><td>{_esc(label)}</td><td colspan='3'>data unavailable</td></tr>"
        cls = "up" if (chg or 0) >= 0 else "down"
        return (
            f"<tr><td>{_esc(label)}</td><td>{price:,.2f}</td>"
            f"<td class='{cls}'>{chg:+.2f}%</td><td>prev close {prev:,.2f}</td></tr>"
        )
    except Exception as exc:  # noqa: BLE001 - show the failure, never fake data
        return f"<tr><td>{_esc(label)}</td><td colspan='3'>data unavailable: {_esc(exc)}</td></tr>"


def home(lang: str) -> str:
    t = lambda k: i18n_mod.t(k, lang)
    rows = "".join(
        [
            _quote_row("^NSEI", "NIFTY 50"),
            _quote_row("^NSEBANK", "BANK NIFTY"),
            _quote_row("^BSESN", "SENSEX"),
        ]
    )
    today = datetime.now(IST).date()
    try:
        summary = paper_mod.day_summary(today)
        paper_html = (
            f"<p>{t('paper_trading')}: {summary.get('trades', 0)} trade(s) today, "
            f"realised ₹{summary.get('realised_pnl', 0):,.2f}, "
            f"risk taken ₹{summary.get('risk_taken', 0):,.2f}, "
            f"flat: {summary.get('flat')}</p>"
        )
    except Exception as exc:  # noqa: BLE001
        paper_html = f"<p>{t('paper_trading')}: unavailable ({_esc(exc)})</p>"
    try:
        news = _cached("news:NIFTY", 300, lambda: news_mod.check_symbol("NIFTY"))
        # "latest" = most recent fetched headlines (bot checked, shown
        # automatically); "items" = only the new/unseen ones. Showing
        # "latest" keeps headlines visible on every load — the bot checks,
        # the user never has to.
        latest = news.get("latest", [])[:5]
        fresh = news.get("items", []) or []
        sources = news.get("sources", {}) or {}
        source_line = (
            f"Bot ne abhi check kiya — NSE announcements: "
            f"{sources.get('nse_announcements', 'unavailable')}; "
            f"Yahoo headlines: {sources.get('yahoo_headlines', 'unavailable')}."
        )
        if latest:
            lis = "".join(
                f"<li>{_esc(it.get('title', ''))} "
                f"[{_esc(it.get('source', ''))}"
                + (f", {', '.join(it.get('tags', []))}" if it.get("tags") else "")
                + "]</li>"
                for it in latest
            )
            fresh_line = (
                f"<p>{len(fresh)} fresh item(s) since last check "
                f"(moving &ge;3%: {news.get('moving')}).</p>"
                if fresh
                else f"<p>No new items since last check (moving &ge;3%: {news.get('moving')}).</p>"
            )
            news_html = (
                f"<h3>{t('news')}</h3><p class='muted'>{_esc(source_line)}</p>"
                f"<ul>{lis}</ul>{fresh_line}"
            )
        else:
            news_html = (
                f"<h3>{t('news')}</h3><p class='muted'>{_esc(source_line)}</p>"
                f"<p>News unavailable right now — no headlines returned "
                f"(moving &ge;3%: {news.get('moving')}).</p>"
            )
    except Exception as exc:  # noqa: BLE001
        news_html = f"<h3>{t('news')}</h3><p>News unavailable ({_esc(exc)})</p>"
    body = (
        f"<table><tr><th>Index</th><th>Price</th><th>Change</th><th></th></tr>{rows}</table>"
        f"{paper_html}{news_html}"
        f"<p>{t('flat_by')}: 15:15 IST. ORB range 09:15–09:30 IST.</p>"
    )
    return _page(lang, t("home"), body)


def orb_page(lang: str) -> str:
    t = lambda k: i18n_mod.t(k, lang)
    watchlist = ROOT / "data" / "watchlist.json"
    result = None
    if watchlist.exists() and time.time() - watchlist.stat().st_mtime < 300:
        try:
            result = json.loads(watchlist.read_text())
        except Exception:  # noqa: BLE001
            result = None
    if result is None:
        try:
            result = _cached("orb", 120, scanner_mod.scan_orb)
            scanner_mod.write_watchlist(result)
        except Exception as exc:  # noqa: BLE001
            return _page(lang, t("orb_scanner"), f"<p>scan unavailable: {_esc(exc)}</p>")
    hits = result.get("hits", [])
    failed = result.get("failed", [])
    rows = "".join(
        f"<tr><td>{_esc(h.get('symbol', ''))}</td><td>{_esc(h.get('direction', ''))}</td>"
        f"<td>{_esc(h.get('close', ''))}</td><td>{_esc(h.get('bar_time', ''))}</td>"
        f"<td>{_esc(h.get('range_high', ''))} – {_esc(h.get('range_low', ''))}</td></tr>"
        for h in hits[:50]
    )
    failed_html = ""
    if failed:
        failed_html = "<p class='muted'>Not scanned (partial coverage): " + ", ".join(
            _esc(f) for f in failed
        ) + "</p>"
    body = (
        f"<p>Fetched {result.get('fetched')} symbols, usable {result.get('usable')}, "
        f"hits {len(hits)} (as of {_esc(result.get('as_of', ''))}). "
        f"A hit is the first completed 5m bar that CLOSES above/below the 09:15–09:30 range — "
        f"wicks do not count, and a hit is stale once the price has moved on.</p>"
        f"<table><tr><th>Symbol</th><th>Direction</th><th>Close</th><th>Bar</th>"
        f"<th>Range high – low</th></tr>{rows}</table>{failed_html}"
    )
    return _page(lang, t("orb_scanner"), body)


def levels_page(lang: str) -> str:
    t = lambda k: i18n_mod.t(k, lang)
    try:
        result = _cached("levels", 60, levels_mod.scan_levels)
    except Exception as exc:  # noqa: BLE001
        return _page(lang, t("levels"), f"<p>levels unavailable: {_esc(exc)}</p>")
    rows = result.get("levels", [])
    failed = result.get("failed", [])

    def _fmt(value) -> str:
        return f"{value:,.2f}" if value is not None else "—"

    def _sl(row: dict) -> str:
        stop = row.get("stop")
        if stop is None:
            return "—"
        atr_stop = row.get("atr_stop")
        if atr_stop is not None:
            return f"{stop:,.2f} (1.5x ATR {_fmt(atr_stop)})"
        return f"{stop:,.2f}"

    body_rows = "".join(
        f"<tr><td>{_esc(r.get('symbol', ''))}</td><td>{_fmt(r.get('price'))}</td>"
        f"<td>{_fmt(r.get('nearest_support'))}</td><td>{_fmt(r.get('nearest_resistance'))}</td>"
        f"<td>{_fmt(r.get('pdh'))}</td><td>{_fmt(r.get('pdl'))}</td>"
        f"<td>{_fmt(r.get('entry'))}</td><td>{_sl(r)}</td>"
        f"<td>{_fmt(r.get('target'))}</td><td>{_esc(r.get('signal') or 'None')}</td></tr>"
        for r in rows
    )
    failed_html = ""
    if failed:
        failed_html = "<p class='muted'>Not computed (partial coverage): " + ", ".join(
            _esc(f"{f['symbol']}: {f['reason']}") for f in failed
        ) + "</p>"
    note = (
        "<p class='muted'>Levels come from Yahoo 5m bars (last 5 sessions) + "
        "daily bars — public, delayed, a proxy; verify on the live platform "
        "before any order. Support/resistance = recent swing highs/lows, "
        "previous-day high/low (PDH/PDL) and today's 09:15-09:30 opening range. "
        "ORB entry = first completed 5m bar closing outside the range; "
        "SL = range midpoint (1.5x ATR stop shown alongside); TP = 2R; flat by "
        "15:15 IST. A signal is stale once the price has moved on. "
        "The NSE option chain is often blocked (HTTP 403) — no chain data is "
        "faked here; see /chain.</p>"
    )
    body = (
        f"<p>As of {_esc(result.get('run_at_ist', ''))}. One row per index — "
        f"enter here, exit here, support here, resistance here.</p>"
        f"<table><tr><th>Symbol</th><th>Price</th><th>Nearest Support</th>"
        f"<th>Nearest Resistance</th><th>PDH</th><th>PDL</th><th>ORB Entry</th>"
        f"<th>SL</th><th>TP</th><th>Signal</th></tr>{body_rows}</table>"
        f"{failed_html}{note}"
    )
    return _page(lang, t("levels"), body)


def skills_page(lang: str) -> str:
    t = lambda k: i18n_mod.t(k, lang)
    try:
        rows = _cached("skills", 120, lambda: skills_mod.collect_skill_status(live=True))
    except Exception as exc:  # noqa: BLE001
        return _page(lang, t("skills"), f"<p>skills unavailable: {_esc(exc)}</p>")
    body_rows = "".join(
        f"<tr><td>{_esc(r.get('daytrader', ''))}</td><td>{_esc(r.get('module', ''))}</td>"
        f"<td>{_esc(r.get('does', ''))}</td><td>{_esc(r.get('shows', ''))}</td>"
        f"<td>{_esc(r.get('status', ''))}</td></tr>"
        for r in rows
    )
    note = (
        "<p class='muted'>All 11 DayTrader skills, in one place. "
        "trader-budget was folded into the sizing calculator (budget is an "
        "input, not a skill of its own — Bhanu's call) and trader-pelosi was "
        "skipped on purpose (US Congress disclosures only, no Indian "
        "equivalent). A skill whose live data cannot be fetched says so "
        "plainly — nothing is faked. The Monte Carlo summary is a small "
        "replay (200 runs); the full 10,000-run check is on the CLI.</p>"
    )
    body = (
        f"<p>{len(rows)} skills — what each does, where it shows, and its live status right now.</p>"
        f"<table><tr><th>DayTrader skill</th><th>Our module</th><th>What it does</th>"
        f"<th>Shows on</th><th>Live status</th></tr>{body_rows}</table>{note}"
    )
    return _page(lang, t("skills"), body)


def timemachine_page(lang: str) -> str:
    t = lambda k: i18n_mod.t(k, lang)
    try:
        # Monte Carlo (10,000 replays) + backtest are cached 10 min:
        # the backtest refetches 1 month of 5m bars, so it is slow.
        data = _cached("timemachine", 600, timecheck_mod.collect_timemachine)
    except Exception as exc:  # noqa: BLE001
        return _page(lang, t("timemachine"), f"<p>time machine unavailable: {_esc(exc)}</p>")

    def _fmt(value) -> str:
        return f"{value:,.2f}" if value is not None else "—"

    setup = data.get("setup")
    setup_html = ""
    if setup:
        setup_html = (
            f"<h3>Today's best ORB setup</h3>"
            f"<p>{_esc(setup.get('symbol', ''))} {_esc(setup.get('signal', ''))} — "
            f"entry {_fmt(setup.get('entry'))}, stop {_fmt(setup.get('stop'))}"
            + (f" (1.5x ATR {_fmt(setup.get('atr_stop'))})" if setup.get("atr_stop") else "")
            + f", target {_fmt(setup.get('target'))}, "
            f"range {_fmt(setup.get('orb_low'))}–{_fmt(setup.get('orb_high'))}, "
            f"price now {_fmt(setup.get('price'))}.</p>"
        )
    else:
        setup_html = f"<h3>Today's best ORB setup</h3><p>{_esc(data.get('setup_error', 'no setup'))}</p>"

    mc = data.get("monte_carlo", {})
    if mc.get("available"):
        mc_html = (
            f"<h3>Monte Carlo Time Machine — {mc['n']:,} replays</h3>"
            f"<p>{_esc(mc.get('symbol', ''))} {_esc(mc.get('direction', ''))} "
            f"entry {_fmt(mc.get('entry'))}, stop {_fmt(mc.get('stop'))}, "
            f"target {_fmt(mc.get('target'))}.</p>"
            f"<table><tr><th>Outcome</th><th>Share</th></tr>"
            f"<tr><td>Target first (win)</td><td>{mc['target_first_pct']:.1f}%</td></tr>"
            f"<tr><td>Stop first (loss)</td><td>{mc['stop_first_pct']:.1f}%</td></tr>"
            f"<tr><td>Neither (flat by 15:15)</td><td>{mc['neither_pct']:.1f}%</td></tr></table>"
            f"<p>Average outcome: {mc['avg_r']:+.2f}R. Best case (target hit): "
            f"+{mc['best_case_r']:.1f}R. Worst case (stop hit): {mc['worst_case_r']:.1f}R. "
            f"Verdict: {mc['verdict']} (break-even {mc['break_even_pct']:.1f}% "
            f"target-first among decisive replays). "
            f"Reality check only — resampled bars cannot see the future, and "
            f"fees/slippage are ignored.</p>"
        )
    else:
        mc_html = (
            f"<h3>Monte Carlo Time Machine</h3>"
            f"<p>Unavailable — {_esc(mc.get('reason', 'no data'))}</p>"
        )

    tc = data.get("trade_check", {})
    if tc.get("available"):
        lim = tc["limits"]
        tc_html = (
            f"<h3>Trade check — {tc['symbol']} {tc['direction']}</h3>"
            f"<p>Risk gate: <b>{tc['gate_verdict']}</b>"
            + (f" — {_esc(tc['gate_reason'])}" if tc.get("gate_reason") and tc["gate_reason"] != "ok" else "")
            + f". Sizing: {tc['sizing_verdict']} — {_esc(tc['sizing_reason'])}.</p>"
            f"<table><tr><th>Four limits (max units)</th><th>Units</th></tr>"
            f"<tr><td>Max loss/trade (₹500 / risk per unit)</td><td>{lim['max_loss']}</td></tr>"
            f"<tr><td>Position cap (75% of budget / entry)</td><td>{lim['position_cap']}</td></tr>"
            f"<tr><td>Max order (₹80,000 / entry)</td><td>{lim['max_order']}</td></tr>"
            f"<tr><td>Cash buffer (90% of budget / entry)</td><td>{lim['cash_buffer']}</td></tr>"
            f"<tr><td><b>Binding limit → qty</b></td><td><b>{tc['qty']}</b> ({_esc(tc.get('binding_limit', ''))})</td></tr></table>"
            f"<p>Entry {_fmt(tc.get('entry'))}, stop {_fmt(tc.get('stop'))}, "
            f"target {_fmt(tc.get('target'))}, R:R {tc['rr']:.2f}. "
            f"Position value ₹{tc['position_value']:,.2f}, max loss at stop "
            f"₹{tc['max_loss']:,.2f}. Positions: capital allows "
            f"{tc['positions_by_capital']}, day limit allows "
            f"{tc['positions_by_day']}/day — max {tc['positions_max']} such "
            f"position(s) (budget ₹{tc['budget']:,.0f}).</p>"
        )
    else:
        tc_html = (
            f"<h3>Trade check</h3>"
            f"<p>Unavailable — {_esc(tc.get('reason', 'no setup'))}</p>"
        )

    bt = data.get("backtest", {})
    bt_rows = ""
    if bt.get("available"):
        for direction in ("long", "short"):
            d = bt["directions"].get(direction, {})
            if d.get("available"):
                bt_rows += (
                    f"<tr><td>ORB {direction}</td><td>{d['fires']}</td>"
                    f"<td>{d['target_wins']}</td><td>{d['stop_losses']}</td>"
                    f"<td>{d['neither']}</td><td>{d['avg_r']:+.3f}R</td></tr>"
                )
            else:
                bt_rows += (
                    f"<tr><td>ORB {direction}</td><td colspan='5'>unavailable — "
                    f"{_esc(d.get('reason', ''))}</td></tr>"
                )
        bt_html = (
            f"<h3>ORB backtest — NIFTY 5m ({_esc(bt.get('first_day', '?'))} to "
            f"{_esc(bt.get('last_day', '?'))}, {_esc(bt.get('sessions', '?'))} full sessions)</h3>"
            f"<table><tr><th>Direction</th><th>Fires</th><th>Target wins</th>"
            f"<th>Stop losses</th><th>Neither</th><th>Avg R</th></tr>{bt_rows}</table>"
            f"<p class='muted'>History replay on real bars (range-midpoint stop, "
            f"2R target, flat 15:15; no fees/slippage). Small sample, and the "
            f"period was a strong uptrend — do not read this as a general edge. "
            f"No accuracy percentage is claimed.</p>"
        )
    else:
        bt_html = (
            f"<h3>ORB backtest</h3>"
            f"<p>Unavailable — {_esc(bt.get('reason', 'no data'))}</p>"
        )

    body = setup_html + mc_html + tc_html + bt_html
    return _page(lang, t("timemachine"), body)


def chain_page(lang: str, query: dict) -> str:
    t = lambda k: i18n_mod.t(k, lang)
    symbol = (query.get("symbol") or ["NIFTY"])[0].upper()
    if symbol not in ("NIFTY", "BANKNIFTY"):
        symbol = "NIFTY"
    try:
        chain = _cached(f"chain:{symbol}", 60, lambda: options_mod.fetch_chain(symbol))
    except Exception as exc:  # noqa: BLE001
        return _page(lang, t("option_chain"), f"<p>unavailable: {_esc(exc)}</p>")
    if "error" in chain:
        return _page(
            lang,
            t("option_chain"),
            f"<p>Option chain unavailable: {_esc(chain['error'])}</p>"
            f"<p><a href='/chain?symbol=NIFTY&lang={lang}'>NIFTY</a> | "
            f"<a href='/chain?symbol=BANKNIFTY&lang={lang}'>BANKNIFTY</a></p>",
        )
    summary = options_mod.pcr_summary(chain)
    strikes = chain.get("strikes", [])[:30]
    rows = "".join(
        f"<tr><td>{_esc(s.get('strike', ''))}</td>"
        f"<td>{_esc(s.get('ce', {}).get('lastPrice', ''))}</td>"
        f"<td>{_esc(s.get('ce', {}).get('openInterest', ''))}</td>"
        f"<td>{_esc(s.get('pe', {}).get('lastPrice', ''))}</td>"
        f"<td>{_esc(s.get('pe', {}).get('openInterest', ''))}</td></tr>"
        for s in strikes
    )
    body = (
        f"<p><a href='/chain?symbol=NIFTY&lang={lang}'>NIFTY</a> | "
        f"<a href='/chain?symbol=BANKNIFTY&lang={lang}'>BANKNIFTY</a></p>"
        f"<p>Underlying: {summary.get('underlying')} | ATM: {summary.get('atm')} | "
        f"PCR (PE OI / CE OI): {summary.get('pcr')} | "
        f"CE OI: {summary.get('ce_oi')} | PE OI: {summary.get('pe_oi')}</p>"
        f"<table><tr><th>Strike</th><th>CE LTP</th><th>CE OI</th>"
        f"<th>PE LTP</th><th>PE OI</th></tr>{rows}</table>"
    )
    return _page(lang, f"{t('option_chain')} — {symbol}", body)


def sizing_page(lang: str, query: dict) -> str:
    t = lambda k: i18n_mod.t(k, lang)

    def _f(name: str, default: float) -> float:
        try:
            return float((query.get(name) or [default])[0])
        except (TypeError, ValueError):
            return default

    entry = _f("entry", 226.55)
    stop = _f("stop", 176.55)
    target = _f("target", 0.0) or None
    budget = _f("budget", 100000.0)
    max_loss = _f("max_loss", 500.0)
    cfg = sizing_mod.default_config(budget=budget, max_loss_per_trade=max_loss)
    report = sizing_mod.size_report(entry, stop, cfg, target=target)
    form = (
        f"<form method='get' action='/sizing'>"
        f"<input type='hidden' name='lang' value='{lang}'>"
        f"Entry ₹ <input name='entry' value='{entry}' size='10'> "
        f"Stop ₹ <input name='stop' value='{stop}' size='10'> "
        f"Target ₹ <input name='target' value='{target or ''}' size='10'> "
        f"Budget ₹ <input name='budget' value='{budget:.0f}' size='10'> "
        f"Max loss ₹ <input name='max_loss' value='{max_loss:.0f}' size='8'> "
        f"<button type='submit'>{t('sizing')}</button></form>"
    )
    pre = f"<pre>{_esc(sizing_mod.format_report(report, cfg))}</pre>"
    note = (
        "<p class='muted'>Budget is an input to this calculator, not a skill of its own. "
        "Size = smallest of four limits (max loss / risk, position cap, max order, cash buffer). "
        "If the stop is too far for the max loss, the calculator refuses.</p>"
    )
    return _page(lang, t("sizing"), form + pre + note)


def paper_page(lang: str) -> str:
    t = lambda k: i18n_mod.t(k, lang)
    try:
        positions = paper_mod.list_positions()
        trades = paper_mod.list_trades()
        today = datetime.now(IST).date()
        summary = paper_mod.day_summary(today)
    except Exception as exc:  # noqa: BLE001
        return _page(lang, t("paper_trading"), f"<p>unavailable: {_esc(exc)}</p>")
    pos_rows = "".join(
        f"<tr><td>{_esc(p.get('id', ''))}</td><td>{_esc(p.get('symbol', ''))}</td>"
        f"<td>{_esc(p.get('direction', ''))}</td><td>{_esc(p.get('qty', ''))}</td>"
        f"<td>{_esc(p.get('entry', ''))}</td><td>{_esc(p.get('stop', ''))}</td>"
        f"<td>{_esc(p.get('target', ''))}</td></tr>"
        for p in positions
    )
    tr_rows = "".join(
        f"<tr><td>{_esc(tr.get('id', ''))}</td><td>{_esc(tr.get('symbol', ''))}</td>"
        f"<td>{_esc(tr.get('direction', ''))}</td><td>{_esc(tr.get('exit_price', ''))}</td>"
        f"<td>{_esc(tr.get('realised_pnl', ''))}</td><td>{_esc(tr.get('reason', ''))}</td></tr>"
        for tr in trades[-20:]
    )
    body = (
        f"<p>Today: {summary.get('trades', 0)} trade(s), realised "
        f"₹{summary.get('realised_pnl', 0):,.2f}, risk taken "
        f"₹{summary.get('risk_taken', 0):,.2f}, flat: {summary.get('flat')}</p>"
        f"<h3>Open positions</h3><table><tr><th>ID</th><th>Symbol</th><th>Dir</th>"
        f"<th>Qty</th><th>Entry</th><th>Stop</th><th>Target</th></tr>{pos_rows or '<tr><td colspan=7>none</td></tr>'}</table>"
        f"<h3>Closed trades</h3><table><tr><th>ID</th><th>Symbol</th><th>Dir</th>"
        f"<th>Exit</th><th>P&amp;L</th><th>Reason</th></tr>{tr_rows or '<tr><td colspan=6>none</td></tr>'}</table>"
    )
    return _page(lang, t("paper_trading"), body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the console quiet
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        lang = _lang(query)
        try:
            if parsed.path in ("/", ""):
                body = home(lang)
            elif parsed.path == "/orb":
                body = orb_page(lang)
            elif parsed.path == "/levels":
                body = levels_page(lang)
            elif parsed.path == "/chain":
                body = chain_page(lang, query)
            elif parsed.path == "/sizing":
                body = sizing_page(lang, query)
            elif parsed.path == "/paper":
                body = paper_page(lang)
            elif parsed.path == "/skills":
                body = skills_page(lang)
            elif parsed.path == "/timemachine":
                body = timemachine_page(lang)
            else:
                body = _page(lang, "Not found", "<p>Unknown page.</p>")
                self._send(body, 404)
                return
            self._send(body, 200)
        except Exception as exc:  # noqa: BLE001 - never crash the server on a page
            self._send(_page(lang, "Error", f"<p>Something went wrong: {_esc(exc)}</p>"), 500)

    def _send(self, body: str, status: int):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Bhanu Indian Day Trader running at http://localhost:{PORT} (paper trading only)")
    server.serve_forever()


if __name__ == "__main__":
    main()
