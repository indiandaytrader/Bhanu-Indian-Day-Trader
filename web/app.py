# web/app.py
"""Bhanu Indian Day Trader — web dashboard.

Stdlib-only HTTP server (no framework). Pages:
  /         home — index quotes + intraday charts, Smart Money Flow,
            sector map, news feed, ORB hits, robot status, levels and
            Time Machine summaries (charts on top, tables below)
  /api/poller, /api/robot  live-engine status + robot card (in-memory,
            instant — these never fetch)
  /orb      ORB scanner + candlestick chart with overlays (VWAP, OB/FVG,
            structure, entry/SL/TP boxes, PDH/PDL)
  /levels   live support/resistance + candlestick chart with overlays
  /sectors  sector map (bubble chart) + sector bar list
  /chain    Nifty / Bank Nifty option chain with OI + PCR
  /sizing   position sizing calculator (four limits, budget is an input)
  /paper    paper trading ledger
  /skills   all 11 DayTrader skills — what each does + live status + news
  /timemachine  Monte Carlo Time Machine (distribution chart) + trade
            check + ORB backtest
  /api/flow, /api/sectors  JSON for the lazy-loaded flow/sector charts
  /api/quotes, /api/intraday, /api/orbchart, /api/levelschart,
  /api/timemachine, /api/news  JSON for the 60-second auto-refresh
  /api/orb, /api/orbbreakdown, /api/levels, /api/levelsbreakdown
            HTML fragments for the lazy-loaded scan tables and signal
            breakdowns (home, /orb, /levels)
  /static/lightweight-charts.js  the chart library, bundled locally
            (no CDN dependency — charts render even when the CDN is
            slow or blocked)

Pages render INSTANTLY from local data only: a background live engine
(``bhanu.poller``) keeps quotes, intraday bars, flow, sectors, news,
scans and the Time Machine warm in memory, so every network fetch
happens in the background, never on a page load. Each section loads
lazily from /api/* after the page, with a "loading" state until the
data arrives. When the engine has nothing yet, the page falls back to
the on-demand fetchers (same caches as before: quotes/charts 60s,
flow/sector 5 min, the Time Machine 10 min) — a slow source delays
one card, never the page. When a source is unavailable the page says
so plainly — no fake data is ever shown.

Charts render with TradingView Lightweight Charts, bundled locally
(web/static/lightweight-charts.js); when the library cannot load, a
built-in SVG line/candlestick chart is drawn instead — a chart box
is never left blank.
REAL-TIME, not refresh: index quotes and the home line charts tick
every 2 seconds — the background engine sparks the 3 indices every
2 s and the browser updates each chart's last point in place
(``/api/live``), so the line visibly moves while you watch. No page
reload, no 60-second wait. The LIVE indicator updates on every
successful tick. Slower sections (flow, sectors, news, ORB scans)
sync in place on their own cadence (60 s / 2–5 min) because their
data only changes that often — they never ask for a reload either.
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

from bhanu import charts as charts_mod
from bhanu import config as config_mod
from bhanu import data as data_mod
from bhanu import i18n as i18n_mod
from bhanu import levels as levels_mod
from bhanu import range_replay
from web.detail_jobs import DetailJobs
from bhanu import greeks
from bhanu import options as options_mod
from bhanu import paper as paper_mod
from bhanu import scanner as scanner_mod
from bhanu import signal as signal_mod
from bhanu import sizing as sizing_mod
from bhanu import skills as skills_mod
from bhanu import stock as stock_mod
from bhanu import timecheck as timecheck_mod
from bhanu import volume as volume_mod
from bhanu import news as news_mod
from bhanu import poller as poller_mod

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("PORT", "8810"))

#: Auto-refresh interval for chart pages (milliseconds). Every 60s each
#: registered chart re-fetches its data and updates in place — no page
#: reload. Server-side caches are 30–600s, so a tick usually returns
#: fresh-enough data without hammering Yahoo/NSE.
LIVE_REFRESH_MS = 60_000

_detail_jobs = DetailJobs()

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


def _opt(direction) -> str:
    """User-facing option label for a trade direction: CALL (green) for
    a long, PUT (red) for a short. Display only — the internal
    direction stays 'long'/'short' everywhere (API JSON, logic,
    sizing, poller)."""
    if str(direction).lower() == "short":
        return "<span class='opt-put'>PUT</span>"
    if str(direction).lower() == "long":
        return "<span class='opt-call'>CALL</span>"
    return _esc(direction)


def _opt_text(direction) -> str:
    """Plain-text CALL/PUT label for contexts that cannot render HTML
    (e.g. JS textContent). Same mapping as _opt."""
    if str(direction).lower() == "short":
        return "PUT"
    if str(direction).lower() == "long":
        return "CALL"
    return str(direction)


#: TradingView Lightweight Charts (pinned version). The library is
#: bundled locally (web/static/lightweight-charts.js, Apache 2.0) so
#: charts render even when the CDN is slow or blocked — a chart is
#: never blank because of the network. The CDN is only a fallback
#: when the local file is missing from disk.
CHART_CDN = (
    "https://unpkg.com/lightweight-charts@4.2.0"
    "/dist/lightweight-charts.standalone.production.js"
)
CHART_LOCAL = "lightweight-charts.js"


def _page(lang: str, title: str, body: str, head_extra: str = "", scripts: str = "",
          live: bool = False, active: str = "") -> str:
    """Page shell. ``live=True`` adds the LIVE indicator + auto-refresh
    engine (chart pages only — static pages never claim to be live).
    The header is a professional tab bar (segmented control, active
    tab highlighted) — never plain text links."""
    t = lambda k: i18n_mod.t(k, lang)
    live_html = ""
    if live:
        live_html = (
            "<p class='muted' id='bhanu-live-indicator' "
            "title='Live — quotes and index charts tick every 2 seconds, no page reload'>"
            "<span class='dot'></span> LIVE · last update --:--:--</p>"
        )
        scripts = scripts + _live_js()

    def _tab(key: str, href: str, label: str) -> str:
        on = " class='on'" if active == key else ""
        return f"<a href='{href}'{on}>{label}</a>"

    nav = (
        f"<nav class='seg'>"
        f"{_tab('home', f'/?lang={lang}', t('home'))}"
        f"{_tab('orb', f'/orb?lang={lang}', t('orb_scanner'))}"
        f"{_tab('levels', f'/levels?lang={lang}', t('levels'))}"
        f"{_tab('sectors', f'/sectors?lang={lang}', 'Sectors')}"
        f"{_tab('chain', f'/chain?lang={lang}', t('option_chain'))}"
        f"{_tab('sizing', f'/sizing?lang={lang}', t('sizing'))}"
        f"{_tab('paper', f'/paper?lang={lang}', t('paper_trading'))}"
        f"{_tab('skills', f'/skills?lang={lang}', t('skills'))}"
        f"{_tab('timemachine', f'/timemachine?lang={lang}', t('timemachine'))}"
        f"</nav>"
        f"<span class='lang'><a href='?lang=en'>EN</a><a href='?lang=hi'>HI</a></span>"
    )
    return f"""<!DOCTYPE html>
<html lang="en" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} — Bhanu Indian Day Trader</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700&family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;600;700&display=swap" rel="stylesheet" media="print" onload="this.media='all'">
<style>
:root{{--bg:#edf0f3;--card:#ffffff;--text:#141a26;--muted:#6b7385;--border:#d9dee6;
--accent:#2563eb;--up:#2f9e5f;--down:#d6455d;--upbg:#e7f5ec;--downbg:#fbe9ec;
--upmid:#9fd8b5;--downmid:#f0a9b5;--blue:#2563eb;--gold:#b7791f;--th:#f1f4f8;--grid:#eef1f5}}
html[data-theme="dark"]{{--bg:#121417;--card:#1c1f24;--text:#e8e8e8;--muted:#9aa0a6;
--border:#333;--accent:#7aa2ff;--up:#2f9e5f;--down:#d6455d;--upbg:#173425;--downbg:#3a1d22;
--upmid:#2f9e5f;--downmid:#d6455d;--blue:#7aa2ff;--gold:#b7791f;--th:#24272c;--grid:#2a2d33}}
body{{font:13px/1.4 Inter,system-ui,sans-serif;max-width:1400px;margin:0 auto;padding:0 22px 22px;
background:var(--bg);color:var(--text)}}
.mono{{font-family:"JetBrains Mono",monospace}}
h1{{font:700 26px "Barlow Condensed",sans-serif;letter-spacing:.5px;margin:14px 0 4px}}
h2{{font:700 19px "Barlow Condensed",sans-serif;letter-spacing:.6px;text-transform:uppercase;margin:12px 0 4px}}
h3{{font:700 17px "Barlow Condensed",sans-serif;letter-spacing:.4px;text-transform:uppercase}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid var(--border);
padding:6px 8px;text-align:left;overflow-wrap:break-word}}th{{background:var(--th)}}
.table-wrap{{overflow-x:auto;max-width:100%;box-sizing:border-box}}
.table-wrap>table{{table-layout:fixed;width:100%}}
pre{{overflow-x:auto;max-width:100%;box-sizing:border-box}}
.up{{color:var(--up)}} .down{{color:var(--down)}} .muted{{color:var(--muted)}}
#ml-disc{{position:sticky;top:0;z-index:50;background:#fff7e0;border-bottom:1px solid #f1d58a;
color:#6b4e00;font:12px Inter,system-ui,sans-serif;padding:7px 22px;text-align:center;margin:0 -22px}}
.ml-top{{display:flex;align-items:center;gap:12px;margin:12px 0 8px}}
.ml-top h1{{margin:0}}
.ml-top .sp{{flex:1}}
nav.seg{{margin:4px 0 12px}}
nav.seg a{{display:inline-block;padding:6px 12px;font:600 12px Inter,sans-serif;color:var(--text);text-decoration:none}}
nav.seg a:hover{{color:var(--accent)}}
.lang{{margin-left:8px}}
.lang a{{display:inline-block;border:1px solid var(--border);background:var(--card);border-radius:6px;padding:4px 8px;color:var(--text);text-decoration:none;font-size:12px;font-weight:600;margin-left:4px}}
.lang a:hover{{border-color:var(--accent);color:var(--accent)}}
.pill{{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--border);background:var(--card);
border-radius:999px;padding:4px 11px;font-size:11.5px;font-weight:600}}
.dot{{width:7px;height:7px;border-radius:50%;background:var(--up);display:inline-block;
animation:mlpulse 1.6s infinite}}
@keyframes mlpulse{{0%{{box-shadow:0 0 0 0 rgba(47,158,95,.55)}}70%{{box-shadow:0 0 0 7px rgba(47,158,95,0)}}100%{{box-shadow:0 0 0 0 rgba(47,158,95,0)}}}}
.seg{{display:inline-flex;border:1px solid var(--border);border-radius:8px;overflow:hidden;background:var(--card)}}
.seg button{{border:0;background:none;padding:6px 12px;font:600 12px Inter,sans-serif;color:var(--text);cursor:pointer}}
.seg button.on,nav.seg a.on{{background:#e6edf8;color:var(--accent)}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:8px;
padding:16px;margin:16px 0;box-shadow:0 1px 3px rgba(0,0,0,0.06);
box-sizing:border-box;max-width:100%}}
.card h3{{margin-top:0}}
.ml-h{{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}}
.ml-h .sub{{color:var(--muted);font-size:11.5px}}
.ml-h .sp{{flex:1}}
.ml-h a{{font-size:11.5px;border:1px solid var(--border);background:var(--card);border-radius:6px;
padding:4px 10px;color:var(--text);text-decoration:none}}
.note{{color:var(--muted);font-size:10.5px;margin-top:8px}}
.chart-wrap{{position:relative;width:100%}}
.chart-box{{width:100%;height:300px}}
.chart-box.tall{{height:420px}}
.news-item{{border-left:3px solid var(--accent);padding:8px 12px;margin:8px 0;
background:var(--card)}}
.theme-btn{{float:right;background:var(--card);color:var(--text);
border:1px solid var(--border);border-radius:8px;padding:4px 10px;cursor:pointer}}
.sector-bar{{height:10px;border-radius:5px;background:var(--up)}}
.sector-bar.down{{background:var(--down)}}
.index-cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:12px 0}}
.icard{{background:var(--card);border:1px solid var(--upmid);border-radius:4px;position:relative;
height:76px;overflow:hidden}}
.icard.neg{{border-color:var(--downmid)}}
.icard .spark{{position:absolute;left:0;top:0;width:100%;height:46px}}
.icard .lab{{position:absolute;left:6px;top:4px;background:rgba(255,255,255,.92);padding:2px 6px 3px;border-radius:3px;z-index:2}}
.icard .lab b{{font:700 14px "Barlow Condensed",sans-serif;letter-spacing:.4px;display:block;line-height:1}}
.icard .foot{{position:absolute;left:0;right:0;bottom:0;height:30px;border-top:1px solid #edf0f3;
padding:3px 8px;font-size:11px;display:flex;justify-content:space-between;align-items:center;background:var(--card)}}
.icard .foot b{{font-weight:600;font-family:"JetBrains Mono",monospace}}
.loading{{color:var(--muted);font-style:italic}}
.tag{{display:inline-block;font-size:9.5px;font-weight:700;border:1px solid currentColor;border-radius:3px;padding:0 5px;line-height:14px}}
.tag.bull{{color:var(--up)}}.tag.bear{{color:var(--down)}}
.opt-call{{color:var(--up);font-weight:700}}.opt-put{{color:var(--down);font-weight:700}}
a.icard,a.tile,a.row{{text-decoration:none;color:inherit}}
a.icard{{display:block}}
.tfbtn{{border:1px solid var(--border);background:var(--card);border-radius:6px;padding:4px 10px;font:600 12px Inter,sans-serif;color:var(--text);cursor:pointer;margin-right:4px}}
.tfbtn.on{{background:#e6edf8;color:var(--accent);border-color:var(--accent)}}
.opill{{border:1px solid var(--border);background:var(--card);border-radius:999px;padding:4px 11px;font:600 11.5px Inter,sans-serif;color:var(--text);cursor:pointer;margin:2px 3px 2px 0}}
.opill.on{{background:#e6edf8;color:var(--accent);border-color:var(--accent)}}
.opill:disabled{{opacity:.45;cursor:not-allowed}}
.stock-grid{{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:12px;align-items:start}}
@media (max-width:900px){{.stock-grid{{grid-template-columns:1fr}}}}
.stock-head{{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}}
.stock-head .px{{font:700 22px "JetBrains Mono",monospace}}
.setup-stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:4px;margin:6px 0}}
.setup-stats div{{background:#f1f4f8;border-radius:6px;padding:4px 6px;font-size:10px;color:var(--muted)}}
.setup-stats b{{display:block;font-size:13.5px;color:var(--text);font-family:"JetBrains Mono",monospace}}
.counts{{font-size:9.5px;font-weight:700}}
.counts .n{{font:700 20px "JetBrains Mono",monospace;line-height:1}}
.sm-stage{{display:flex;gap:10px;align-items:stretch}}
.sm-spaghetti{{flex:1;min-width:0;position:relative}}
.sm-waterfall{{width:300px;flex:none;display:flex;flex-direction:column;gap:3px}}
.tile{{position:relative;flex:1;border:1px solid var(--upmid);background:var(--upbg);border-radius:3px;overflow:hidden;min-height:44px}}
.tile.bear{{border-color:var(--downmid);background:var(--downbg)}}
.tile .lab{{position:absolute;left:3px;top:3px;background:rgba(255,255,255,.92);border-radius:2px;padding:1px 4px 2px;z-index:2;max-width:72%}}
.tile .lab b{{font:700 11.5px "Barlow Condensed",sans-serif;letter-spacing:.3px;display:block;line-height:1.05;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.tile .lab span{{font-size:10px}}
.tile .rk{{position:absolute;right:3px;top:3px;border:1px solid currentColor;border-radius:2px;font-size:9px;padding:0 3px;background:#fff;z-index:2}}
.tile .px{{position:absolute;right:0;bottom:0;background:var(--text);color:#fff;font-size:9.5px;font-weight:700;padding:1px 4px;z-index:2}}
.hz-wrap{{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:12px}}
.hz-side h3{{margin:16px 0 6px;font-size:13px;font-weight:700}}
.hz-side h3:first-child{{margin-top:0}}
.hz-side h3 small{{font-weight:500;color:var(--muted);font-size:11px}}
.row{{display:grid;grid-template-columns:20px 1fr 90px 60px;gap:6px;align-items:center;font-size:12.5px;padding:2px 0;border-radius:5px}}
.row:hover{{background:#f3f6fa}}
.bar{{height:8px;border-radius:2px}}
.niftysep{{display:flex;justify-content:space-between;border-top:1.5px dashed var(--blue);color:var(--blue);font-size:11px;font-weight:700;padding-top:2px;margin:3px 0}}
.badge{{font-size:10px;font-weight:800;background:var(--upbg);color:var(--up);border-radius:5px;padding:2px 7px;letter-spacing:.3px}}
.tpanel{{border:1px solid var(--border);border-radius:8px;padding:8px 10px;background:linear-gradient(180deg,#f7f9fd,#fff);margin:8px 0}}
.tpanel .tt{{font-size:10px;color:var(--muted);letter-spacing:.6px;font-weight:700;display:flex;justify-content:space-between;gap:6px}}
.tpanel .tn{{font-weight:700;font-size:12.5px;margin-top:3px}}
.tpanel .tn .muted{{font-weight:500;font-size:11px}}
.tpanel .ts{{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;margin:6px 0}}
.tpanel .ts div{{background:#f1f4f8;border-radius:6px;padding:4px 6px;font-size:10px;color:var(--muted)}}
.tpanel .ts b{{display:block;font-size:13.5px;color:var(--text);font-family:"JetBrains Mono",monospace}}
.tpanel .tr{{display:flex;justify-content:space-between;gap:6px;font-size:10.5px;padding:3px 4px;border-radius:5px;font-family:"JetBrains Mono",monospace}}
.tpanel .tr:hover{{background:#e9f0fb}}
.tpanel .rr{{display:flex;height:7px;border-radius:4px;overflow:hidden;margin-top:5px}}
.tpanel .rr i{{display:block;height:100%}}
.tpanel .rrl{{display:flex;justify-content:space-between;font-size:10px;margin-top:2px;font-family:"JetBrains Mono",monospace}}
.tpanel .st{{border-radius:7px;padding:6px 8px;margin:6px 0;font-size:11px;line-height:1.45}}
.tpanel .st b{{font-size:10.5px;letter-spacing:.3px}}
.tpanel .st.sig{{background:#e8f6ee;border:1px solid #a7dcbc}}
.tpanel .st.none{{background:#f4f6f9;border:1px solid var(--border)}}
.tpanel .st.warn{{background:#fff6dc;border:1px solid #f1d58a}}
.tpanel .chk{{display:flex;flex-direction:column;gap:1px;margin:3px 0;font-size:11px}}
.foot{{color:var(--muted);font-size:10.5px;text-align:center;padding:0 0 14px}}
.divider{{height:1px;background:#d9dee6;margin:8px 0 6px}}
</style><link rel="stylesheet" href="/static/terminal.css">{head_extra}</head>
<body><div id="ml-disc"><b>Paper trading only · not investment advice.</b> Market feeds may be delayed.</div>
<div class='ml-top'><h1>Bhanu Indian Day Trader</h1><span class='sp'></span><time class='session-clock' id='session-clock'></time><span class='paper-mode'>Paper terminal</span><button aria-label='Toggle light or dark theme' class="theme-btn" onclick="bhanuTheme()">Theme</button></div>
<div class='nav-strip'>{nav}</div>
<main><h2>{_esc(title)}</h2>{live_html}{body}</main>
<p class="foot">Paper trading only — no real orders. Educational software, not investment advice.</p>
<script>
const marketClock=new Intl.DateTimeFormat('en-GB',{{timeZone:'Asia/Kolkata',hour:'2-digit',minute:'2-digit',second:'2-digit'}});
function paintClock(){{document.getElementById('session-clock').textContent=marketClock.format(new Date())+' IST';}}
paintClock();setInterval(paintClock,1000);
document.querySelectorAll('.lang a').forEach(a=>{{
  const u=new URL(location.href);u.searchParams.set('lang',new URL(a.href).searchParams.get('lang'));a.href=u.href;
}});
function bhanuTheme(){{
  var h=document.documentElement;
  var next=h.getAttribute('data-theme')==='dark'?'light':'dark';
  h.setAttribute('data-theme',next);
  if(window.bhanuChartOpts){{const o=bhanuChartOpts();for(const st of [...Object.values(window.bhanuLines||{{}}),...Object.values(window.bhanuCandles||{{}})]){{if(st.chart)st.chart.applyOptions({{layout:o.layout,grid:o.grid}});}}}}
  try{{localStorage.setItem('bhanu-theme',next);}}catch(e){{}}
}}
try{{var s=localStorage.getItem('bhanu-theme');
if(s){{document.documentElement.setAttribute('data-theme',s);}}}}catch(e){{}}
</script>
{scripts}
</body></html>"""


def _chart_json(payload) -> str:
    """JSON for embedding in a page — `</` escaped so it can never close
    its own <script> tag."""
    return json.dumps(payload, default=str).replace("</", "<\\/")


def _static_file(name: str) -> tuple[bytes, str] | None:
    """Serve one bundled static file (exact name only — no traversal).

    Returns ``(bytes, content_type)`` or ``None`` when the name is not
    an allowed static file or the file is missing.
    """
    if name not in (CHART_LOCAL, "terminal.css"):
        return None
    path = ROOT / "web" / "static" / name
    try:
        mime = "text/css" if name.endswith(".css") else "application/javascript"
        return path.read_bytes(), mime + "; charset=utf-8"
    except OSError:
        return None


def _chart_lib_tag() -> str:
    """Script tag for the chart library — local bundle first, CDN fallback."""
    if (ROOT / "web" / "static" / CHART_LOCAL).exists():
        return '<script src="/static/lightweight-charts.js"></script>'
    return f'<script src="{CHART_CDN}"></script>'


def _live_js() -> str:
    """Slow-sync engine for chart pages (flow, sectors, news, ORB).

    Every LIVE_REFRESH_MS each registered refresher (a function returning
    a Promise) re-fetches its section data and updates it in place —
    no page reload, no flicker. Ticks are skipped while the tab is hidden
    or a refresh is already in flight; returning to a visible tab
    refreshes immediately. A failed tick keeps the old data on screen and
    the indicator says "update failed, retrying…" with the last good time.
    """
    return """<script>
var BHANU_REFRESH_MS = __LIVE_MS__;
window.bhanuRefreshers = window.bhanuRefreshers || [];
window.bhanuLiveInFlight = false;
function bhanuLiveTime(){
  try{return new Date().toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata',hour12:false});}
  catch(e){return new Date().toLocaleTimeString();}
}
function bhanuLiveClock(){
  // 1-second ticking clock: the LIVE line moves every second so the
  // page never looks frozen while it waits for the next 2-second
  // data tick. Only runs after the first successful data load
  // (data-last set) — before that the indicator says "loading…".
  // The clock shows the current time; "data HH:MM:SS" shows when
  // data last actually arrived.
  var el=document.getElementById('bhanu-live-indicator');
  if(!el){return;}
  if(!el.getAttribute('data-last')){return;}
  if(el.getAttribute('data-failed')==='1'){return;}
  el.innerHTML='<span class="dot"></span> Dashboard · '+bhanuLiveTime()+' · data received '+el.getAttribute('data-last');
}
function bhanuLiveSet(state){
  var el=document.getElementById('bhanu-live-indicator');
  if(!el){return;}
  if(state==='ok'){
    el.setAttribute('data-last',bhanuLiveTime());
    el.removeAttribute('data-failed');
    bhanuLiveClock();
  }else if(state==='fail'){
    var last=el.getAttribute('data-last')||'—';
    el.setAttribute('data-failed','1');
    el.innerHTML='<span class="dot"></span> LIVE · update failed, retrying… (last good '+last+')';
  }
}
function bhanuLiveTick(){
  if(document.hidden){return;}
  if(window.bhanuLiveInFlight){return;}
  if(!window.bhanuRefreshers.length){return;}
  window.bhanuLiveInFlight=true;
  var jobs=window.bhanuRefreshers.map(function(fn){
    try{return Promise.resolve(fn());}catch(e){return Promise.reject(e);}
  });
  var done=function(results){
    window.bhanuLiveInFlight=false;
    var ok=0;results.forEach(function(r){if(r.status==='fulfilled'){ok++;}});
    if(ok>0){bhanuLiveSet('ok');}else{bhanuLiveSet('fail');}
  };
  if(typeof Promise.allSettled==='function'){Promise.allSettled(jobs).then(done);}
  else{Promise.all(jobs).then(function(){done([{status:'fulfilled'}]);},
    function(){done([{status:'rejected'}]);});}
}
function bhanuMarkLive(){
  bhanuLiveSet('ok');
}
function bhanuStartLive(){
  var ind=document.getElementById('bhanu-live-indicator');
  if(!ind){return;}
  // Do NOT claim an update yet — the first data load marks it live
  // (bhanuMarkLive) when it actually succeeds.
  ind.innerHTML='<span class="dot"></span> LIVE · loading…';
  setInterval(bhanuLiveTick, BHANU_REFRESH_MS);
  // The clock ticks every second (see bhanuLiveClock) — the page
  // never looks frozen between 2-second data ticks.
  setInterval(bhanuLiveClock, 1000);
  // First refresh cycle shortly after load, so charts that just
  // finished their initial load also get a prompt 60s-cycle tick.
  setTimeout(bhanuLiveTick, 5000);
  document.addEventListener('visibilitychange',function(){
    if(!document.hidden){bhanuLiveTick();}
  });
}
bhanuStartLive();
</script>""".replace("__LIVE_MS__", str(LIVE_REFRESH_MS))


def _chart_js() -> str:
    """Shared chart helpers: line chart, candlestick + overlays, flow, sectors.

    Every renderer checks for the chart library first (bundled locally
    at /static/lightweight-charts.js); when it cannot load, a built-in
    SVG line/candlestick chart is drawn instead — a chart box is never
    left blank. When the data is missing the card says so honestly.
    Lazy loaders (bhanuLazyLine / bhanuLazyCandle) fetch from /api/* on
    page open, retry with backoff on failure, and mark the LIVE
    indicator when data actually arrives.
    """
    return """<script>
window.bhanuRefreshers = window.bhanuRefreshers || [];
window.bhanuLines = window.bhanuLines || {};
window.bhanuLiveTimers = window.bhanuLiveTimers || {};
window.bhanuCandles = window.bhanuCandles || {};
function bhanuDark(){return document.documentElement.getAttribute('data-theme')==='dark';}
function bhanuLang(){var m=(location.search||'').match(/lang=(en|hi)/);return m?m[1]:'hi';}
function bhanuChartOpts(h){
  var dark=bhanuDark();
  return {width:0,height:h||300,
    layout:{background:{color:'transparent'},textColor:dark?'#c9c9c9':'#333'},
    grid:{vertLines:{color:dark?'#2a2d33':'#eee'},horzLines:{color:dark?'#2a2d33':'#eee'}},
    timeScale:{timeVisible:true,secondsVisible:false}};
}
function bhanuUnavailable(el,why){
  var message=document.createElement('p');message.className='empty-state';
  message.textContent='Chart unavailable — '+why;el.replaceChildren(message);
}
function bhanuSvgLine(el, points, color){
  if(!points||!points.length){bhanuUnavailable(el,'no price data');return;}
  var W=900,H=240,pad=8;
  var vals=points.map(function(p){return p.value;});
  var mn=Math.min.apply(null,vals),mx=Math.max.apply(null,vals);
  if(mx===mn){mx=mn+1;}
  function X(i){return pad+i/(points.length-1||1)*(W-2*pad);}
  function Y(v){return H-pad-(v-mn)/(mx-mn)*(H-2*pad);}
  var d=points.map(function(p,i){return (i?'L':'M')+X(i).toFixed(1)+' '+Y(p.value).toFixed(1);}).join(' ');
  var last=points[points.length-1].value;
  el.innerHTML='<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:auto" role="img" aria-label="Price chart (simple view)"><path d="'+d+'" fill="none" stroke="'+(color||'#2563eb')+'" stroke-width="2"/><circle cx="'+X(points.length-1)+'" cy="'+Y(last)+'" r="4" fill="'+(color||'#2563eb')+'"/><text x="'+(W-8)+'" y="18" text-anchor="end" font-size="13" font-weight="bold" fill="'+(bhanuDark()?'#fff':'#111')+'">'+last.toLocaleString('en-IN')+'</text></svg><p class="muted">Simple chart view — full chart library could not load.</p>';
  // Keep handles so the last point can move in place (bhanuSvgLineLast)
  // instead of rebuilding the whole SVG on every tick.
  el._bhanuPath=el.querySelector('path');
  el._bhanuDot=el.querySelector('circle');
  el._bhanuText=el.querySelector('text');
  el._bhanuPoints=points;
  el._bhanuX=X;
  el._bhanuY=Y;
  // Clamp bounds for the in-place last-point move: a tick outside
  // the initial min/max (common in a trending move) must pin the
  // dot to the chart edge, never fly off the canvas until the
  // 5-minute rebase re-scales the range.
  el._bhanuPad=pad;
  el._bhanuH=H;
}
function bhanuSvgLineLast(el, v){
  // Move only the last point of an existing SVG line chart — the
  // path's last segment, the dot and the price label. No innerHTML
  // rebuild, so the chart never flickers or jumps on a tick.
  if(!el||!el._bhanuPath||!el._bhanuPoints||!el._bhanuPoints.length){return;}
  var pts=el._bhanuPoints;
  pts[pts.length-1].value=v;
  function Yc(val){
    var y=el._bhanuY(val);
    var pad=el._bhanuPad||8, H=el._bhanuH||240;
    return Math.max(pad,Math.min(H-pad,y));
  }
  var d=pts.map(function(p,i){return (i?'L':'M')+el._bhanuX(i).toFixed(1)+' '+Yc(p.value).toFixed(1);}).join(' ');
  el._bhanuPath.setAttribute('d',d);
  if(el._bhanuDot){el._bhanuDot.setAttribute('cx',el._bhanuX(pts.length-1));el._bhanuDot.setAttribute('cy',Yc(v));}
  if(el._bhanuText){el._bhanuText.textContent=Number(v).toLocaleString('en-IN');}
}
function bhanuEase(t){return t<0.5?2*t*t:1-Math.pow(-2*t+2,2)/2;}
function bhanuAnimLast(id, symbol, fromVal, toVal, time){
  // Glide the chart's last point from the old price to the new one
  // (~0.9 s ease) instead of jumping in one step: the line flows
  // instead of stopping for 2 s and then stepping. One animation per
  // chart — a newer tick cancels the older one. The header price
  // glides along too, so the card never looks frozen either.
  var st=window.bhanuLines[id];if(!st){return;}
  if(fromVal===toVal){return;}
  var slug={'^NSEI':'nsei','^NSEBANK':'nsebank','^BSESN':'bsesn'}[symbol];
  var token=(st.animToken||0)+1;st.animToken=token;
  var raf=window.requestAnimationFrame||function(fn){return setTimeout(function(){fn(Date.now());},16);};
  var start=null;
  function frame(ts){
    if(st.animToken!==token){return;}
    if(start===null){start=ts;}
    var k=Math.min(1,(ts-start)/1900);
    var v=fromVal+(toVal-fromVal)*bhanuEase(k);
    if(st.series){try{st.series.update({time:time,value:v});}catch(e){}}
    else if(st.el){
      if(!st.el._bhanuPath){bhanuSvgLine(st.el,st.line||[],st.color||'#2563eb');}
      bhanuSvgLineLast(st.el,v);
    }
    if(slug){var pe=document.getElementById('q-'+slug+'-price');if(pe){pe.textContent=Number(v).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});}}
    if(k<1){raf(frame);}
  }
  raf(frame);
}
function bhanuSvgCandles(el, candles){
  if(!candles||!candles.length){bhanuUnavailable(el,'no price data');return;}
  var W=900,H=340,pad=8;
  var mn=Math.min.apply(null,candles.map(function(c){return c.low;}));
  var mx=Math.max.apply(null,candles.map(function(c){return c.high;}));
  if(mx===mn){mx=mn+1;}
  var n=candles.length, bw=(W-2*pad)/n;
  function Y(v){return H-pad-(v-mn)/(mx-mn)*(H-2*pad);}
  var s='<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:auto" role="img" aria-label="Candlestick chart (simple view)">';
  candles.forEach(function(c,i){
    var x=pad+i*bw+bw*0.2, w=Math.max(1,bw*0.6);
    var col=c.close>=c.open?'#2f9e5f':'#d6455d';
    s+='<line x1="'+(x+w/2).toFixed(1)+'" y1="'+Y(c.high).toFixed(1)+'" x2="'+(x+w/2).toFixed(1)+'" y2="'+Y(c.low).toFixed(1)+'" stroke="'+col+'" stroke-width="1"/>';
    s+='<rect x="'+x.toFixed(1)+'" y="'+Y(Math.max(c.open,c.close)).toFixed(1)+'" width="'+w.toFixed(1)+'" height="'+Math.max(1,Math.abs(Y(c.open)-Y(c.close))).toFixed(1)+'" fill="'+col+'"/>';
  });
  s+='</svg><p class="muted">Simple chart view — full chart library could not load.</p>';
  el.innerHTML=s;
}
function bhanuSvgFlow(el, series){
  // Spaghetti chart: every stock's intraday % move since the open —
  // green above zero, red below, NIFTY dashed blue. Sector labels on
  // the left sit at each sector's median last move.
  if(!series||!series.length){bhanuUnavailable(el,'no flow data');return;}
  var W=960,H=560,padL=64,padR=8,padT=10,padB=26;
  var mn=0,mx=0;
  series.forEach(function(s){s.line.forEach(function(p){if(p.value<mn){mn=p.value;}if(p.value>mx){mx=p.value;}});});
  if(mx-mn<0.5){var mid=(mx+mn)/2;mn=mid-0.25;mx=mid+0.25;}
  var maxLen=Math.max.apply(null,series.map(function(s){return s.line.length;}));
  function X(i){return padL+i/(maxLen-1||1)*(W-padL-padR);}
  function Y(v){return padT+(mx-v)/(mx-mn)*(H-padT-padB);}
  function fmtT(t){try{return new Date(t*1000).toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata',hour:'2-digit',minute:'2-digit',hour12:false});}catch(e){return '';}}
  var s='<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:auto" role="img" aria-label="Smart Money Flow — every stock since the open">';
  var step=1;
  for(var g=Math.ceil(mn/step)*step;g<=mx;g+=step){
    s+='<line x1="'+padL+'" y1="'+Y(g).toFixed(1)+'" x2="'+(W-padR)+'" y2="'+Y(g).toFixed(1)+'" stroke="'+(g===0?'#999':'#e5e7eb')+'" stroke-width="'+(g===0?'1.5':'1')+'"/>';
    s+='<text x="'+(padL-6)+'" y="'+(Y(g)+3.5).toFixed(1)+'" text-anchor="end" font-size="10" fill="#6b7385">'+(g>0?'+':'')+g.toFixed(0)+'%</text>';
  }
  [0,0.25,0.5,0.75,1].forEach(function(f){
    var idx=Math.round(f*(maxLen-1));
    var t=series[0].line[Math.min(idx,series[0].line.length-1)];
    if(t){s+='<text x="'+X(idx).toFixed(1)+'" y="'+(H-8)+'" text-anchor="middle" font-size="10" fill="#6b7385">'+fmtT(t.time)+'</text>';}
  });
  var ordered=series.slice().sort(function(a,b){return (a.is_index?1:0)-(b.is_index?1:0);});
  ordered.forEach(function(ser){
    var col=ser.is_index?'#2563eb':(ser.up?'#2f9e5f':'#d6455d');
    var d=ser.line.map(function(p,i){return (i?'L':'M')+X(i).toFixed(1)+' '+Y(p.value).toFixed(1);}).join(' ');
    var lastV=ser.line[ser.line.length-1].value;
    s+='<a href="/stock?symbol='+encodeURIComponent(ser.symbol)+'&lang='+bhanuLang()+'" style="cursor:pointer"><title>'+ser.symbol+' '+(lastV>=0?'+':'')+lastV.toFixed(2)+'% — detail page kholo</title><path d="'+d+'" fill="none" stroke="'+col+'" stroke-width="'+(ser.is_index?2:1)+'"'+(ser.is_index?' stroke-dasharray="6 4"':'')+' opacity="'+(ser.is_index?1:0.55)+'"/></a>';
  });
  var bySec={};
  series.forEach(function(ser){if(ser.is_index||!ser.sector){return;}var v=ser.line[ser.line.length-1].value;(bySec[ser.sector]=bySec[ser.sector]||[]).push(v);});
  var labs=Object.keys(bySec).map(function(k){var vs=bySec[k].slice().sort(function(a,b){return a-b;});return {name:k,v:vs[Math.floor(vs.length/2)]};}).sort(function(a,b){return b.v-a.v;});
  var lastY=-99;
  labs.forEach(function(l){
    var y=Y(l.v);
    if(y-lastY<14){return;}
    lastY=y;
    s+='<text x="4" y="'+(y+3.5).toFixed(1)+'" font-size="10" font-weight="700" fill="#141a26">'+l.name.toUpperCase()+'</text>';
  });
  var nif=series.filter(function(x){return x.is_index;})[0];
  if(nif){var lv=nif.line[nif.line.length-1].value;s+='<text x="'+(W-padR)+'" y="'+(Y(lv)-6).toFixed(1)+'" text-anchor="end" font-size="11" font-weight="700" fill="#2563eb">NIFTY '+(lv>=0?'+':'')+lv.toFixed(2)+'%</text>';}
  s+='</svg>';
  el.innerHTML=s;
}
function bhanuWaterfall(el, series){
  // Strongest / weakest tiles with mini sparklines (right of spaghetti).
  if(!series||!series.length){el.innerHTML='';return;}
  var sorted=series.slice().filter(function(s){return !s.is_index;}).sort(function(a,b){return b.change_pct-a.change_pct;});
  function tile(s, rank){
    var pts=s.line.slice(-60);
    var W=120,H=30,pad=2;
    var vals=pts.map(function(p){return p.value;});
    var mn=Math.min.apply(null,vals),mx=Math.max.apply(null,vals);
    if(mx===mn){mx=mn+1;}
    function X(i){return pad+i/(pts.length-1||1)*(W-2*pad);}
    function Y(v){return H-pad-(v-mn)/(mx-mn)*(H-2*pad);}
    var d=pts.map(function(p,i){return (i?'L':'M')+X(i).toFixed(1)+' '+Y(p.value).toFixed(1);}).join(' ');
    var col=s.up?'#2f9e5f':'#d6455d';
    return '<a class="tile'+(s.up?'':' bear')+'" href="/stock?symbol='+encodeURIComponent(s.symbol)+'&lang='+bhanuLang()+'"><svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none" style="position:absolute;inset:0;width:100%;height:100%"><path d="'+d+'" fill="none" stroke="'+col+'" stroke-width="1.2" opacity="0.8"/></svg><div class="lab"><b>'+s.symbol+'</b><span>'+(s.sector||'')+'</span></div><span class="rk">'+rank+'</span><span class="px">'+(s.change_pct>=0?'+':'')+s.change_pct.toFixed(2)+'%</span></a>';
  }
  var html='<div class="counts" style="display:flex;gap:14px;margin-bottom:4px"><span class="up">▲ STRONGEST</span><span class="down">▼ WEAKEST</span></div>';
  sorted.slice(0,5).forEach(function(s,i){html+=tile(s,i+1);});
  sorted.slice(-5).reverse().forEach(function(s,i){html+=tile(s,'−'+(i+1));});
  el.innerHTML=html;
}
function bhanuSpark(id, points){
  // Tiny sparkline for an index tile: one thin line, no axes.
  var el=document.getElementById(id);if(!el||!points||points.length<2){return;}
  var pts=points.slice(-120);
  var W=300,H=46,pad=2;
  var vals=pts.map(function(p){return p.value;});
  var mn=Math.min.apply(null,vals),mx=Math.max.apply(null,vals);
  if(mx===mn){mx=mn+1;}
  function X(i){return pad+i/(pts.length-1||1)*(W-2*pad);}
  function Y(v){return H-pad-(v-mn)/(mx-mn)*(H-2*pad);}
  var up=vals[vals.length-1]>=vals[0];
  var col=up?'#2f9e5f':'#d6455d';
  var d=pts.map(function(p,i){return (i?'L':'M')+X(i).toFixed(1)+' '+Y(p.value).toFixed(1);}).join(' ');
  el.innerHTML='<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none" style="width:100%;height:100%;display:block" role="img" aria-label="Sparkline"><path d="'+d+'" fill="none" stroke="'+col+'" stroke-width="1.5"/><circle cx="'+X(pts.length-1)+'" cy="'+Y(vals[vals.length-1])+'" r="2.5" fill="'+col+'"/></svg>';
}
function bhanuSparkFor(symbol, line){
  var map={'^NSEI':'q-nsei-spark','^NSEBANK':'q-nsebank-spark','^BSESN':'q-bsesn-spark'};
  var id=map[symbol];if(!id){return;}
  try{bhanuSpark(id,line);}catch(e){}
}
async function bhanuDetail(url){
  const deadline=Date.now()+180000;
  while(Date.now()<deadline){
    const response=await fetch(url,{headers:{Prefer:'respond-async'},cache:'no-store'});
    if(response.status!==202)return response;
    await new Promise(resolve=>setTimeout(resolve,1200));
  }
  throw Error('Detail feed timed out');
}
function bhanuFetchJson(url, timeoutMs){
  // fetch() with a timeout: a hung request must surface as an error
  // (so the retry loop runs) instead of leaving "loading" forever.
  if(typeof AbortController==='undefined'){
    return fetch(url).then(function(r){
      if(!r.ok){throw new Error('http '+r.status);}return r.json();
    });
  }
  var ctrl=new AbortController();
  var timer=setTimeout(function(){ctrl.abort();},timeoutMs||20000);
  return fetch(url,{signal:ctrl.signal}).then(function(r){
    clearTimeout(timer);
    if(!r.ok){throw new Error('http '+r.status);}return r.json();
  }).catch(function(e){
    clearTimeout(timer);
    if(e&&e.name==='AbortError'){throw new Error('request timed out');}
    throw e;
  });
}
function bhanuTickLabel(el){
  const stamp=Number(el.dataset.tick);
  if(!stamp){el.textContent='2 s poll · source time unavailable';return;}
  const age=Date.now()/1000-stamp;
  const date=new Date(stamp*1000);
  const formatted=date.toLocaleString('en-IN',{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false,timeZone:'Asia/Kolkata'});
  el.textContent='2 s poll · '+(age>=0&&age<120?'Recent tick':'Last known tick')+' · '+formatted+' IST';
}
function bhanuFeedStatus(){
  document.querySelectorAll('[data-tick]').forEach(bhanuTickLabel);
  const badge=document.getElementById('index-feed-state');
  if(!badge)return;
  const allRecent=['chart-nifty-stamp','chart-bank-stamp'].every(id=>{
    const el=document.getElementById(id),age=el?Date.now()/1000-Number(el.dataset.tick):Infinity;
    return age>=0&&age<120;
  });
  badge.textContent=allRecent?badge.dataset.recent:'NSE indices · waiting for recent ticks';
}
bhanuFeedStatus();setInterval(bhanuFeedStatus,2000);
function bhanuLiveLineTick(id, symbol){
  // One live tick: fetch the latest price and glide the line's end
  // to it in place (bhanuAnimLast) — never a full re-fetch, never a
  // jump. When the market clock advanced, the new bar starts at the
  // old value and glides to the new one.
  var st=window.bhanuLines[id];if(!st){return Promise.reject(new Error('no line'));}
  return bhanuFetchJson('/api/live?symbol='+encodeURIComponent(symbol)).then(function(p){
    if(!p||p.error||p.ltp===null||p.ltp===undefined){throw new Error(p&&p.error?p.error:'no live data');}
    var t=p.time||0, v=Number(p.ltp);
    var stamp=document.getElementById(id+'-stamp');
    if(stamp){stamp.dataset.tick=t;bhanuTickLabel(stamp);}
    var from=st.lastValue;
    if(from===undefined||from===null){
      var line0=st.line||[];
      from=line0.length?line0[line0.length-1].value:v;
    }
    if(st.series){
      if(t>st.lastTime){st.lastTime=t;}
      bhanuAnimLast(id,symbol,from,v,st.lastTime);
    }else if(st.el){
      var line=st.line||[];if(!line.length){throw new Error('no line data');}
      var last=line[line.length-1];
      if(t>last.time){line.push({time:t,value:from});}
      bhanuAnimLast(id,symbol,from,v,last.time);
    }
    st.lastValue=v;
    if(typeof bhanuMarkLive==='function'){bhanuMarkLive();}
    bhanuSparkFor(symbol,p.line);
  });
}
function bhanuStartLiveLine(id, symbol){
  // A line chart ticks every 2 seconds once it has drawn. Every 150th
  // tick (~5 minutes) it quietly rebases to fresh 5m bars, so the
  // 1-minute live tail never drifts from the real candles.
  if(!symbol||window.bhanuLiveTimers[id]){return;}
  var ticks=0;
  var run=function(){
    ticks++;
    if(ticks%150===0){
      bhanuFetchJson('/api/intraday?symbol='+encodeURIComponent(symbol)).then(function(p2){
        var st=window.bhanuLines[id];
        if(p2&&!p2.error&&p2.line&&p2.line.length&&st){
          var nl=p2.line.slice();
          if(st.series){st.series.setData(nl);}
          st.line=nl;
          st.lastTime=nl[nl.length-1].time;
          st.lastValue=nl[nl.length-1].value;
          if(st.el){bhanuSvgLine(st.el,nl,st.color||'#2563eb');}
          bhanuSparkFor(symbol,p2.line);
        }
      }).catch(function(){});
      return;
    }
    bhanuLiveLineTick(id,symbol).catch(function(){});
  };
  window.bhanuLiveTimers[id]=setInterval(run,2000);
  run();
}
function bhanuLineRender(id, p, symbol){
  var el=document.getElementById(id);if(!el)return;
  if(!p||!p.line||p.line.length===0){bhanuUnavailable(el,p&&p.error?p.error:'no price data');return;}
  const cacheNote=document.getElementById(id+'-cache-note');
  if(cacheNote)cacheNote.textContent=p.stale?'Stale bars · cache saved '+(p.as_of||'time unavailable'):'';
  var lastTime=p.line[p.line.length-1].time;
  var lastVal=p.line[p.line.length-1].value;
  bhanuSparkFor(symbol,p.line);
  if(typeof LightweightCharts==='undefined'){
    // One array object for both the state and the SVG refs, so the
    // in-place last-point update sees the same points.
    var line=p.line.slice();
    bhanuSvgLine(el,line,p.color||'#2563eb');
    window.bhanuLines[id]={el:el,series:null,line:line,lastTime:lastTime,lastValue:lastVal,color:p.color||'#2563eb'};
    bhanuStartLiveLine(id,symbol);
    return;
  }
  var o=bhanuChartOpts(260);o.width=el.clientWidth||900;
  var ch=LightweightCharts.createChart(el,o);
  var s=ch.addLineSeries({color:p.color||'#2563eb',lineWidth:2});
  s.setData(p.line);ch.timeScale().fitContent();
  window.addEventListener('resize',function(){ch.applyOptions({width:el.clientWidth||900});});
  window.bhanuLines[id]={el:el,chart:ch,series:s,line:p.line.slice(),lastTime:lastTime,lastValue:lastVal,color:p.color||'#2563eb'};
  bhanuStartLiveLine(id,symbol);
}
function bhanuLineChart(id, symbol){
  var el=document.getElementById(id);if(!el)return;
  var raw=document.getElementById(id+'-data');if(!raw){bhanuUnavailable(el,'no chart data');return;}
  var p;try{p=JSON.parse(raw.textContent);}catch(e){bhanuUnavailable(el,'bad chart data');return;}
  bhanuLineRender(id,p,symbol);
}
function bhanuLazyLine(id, symbol){
  var el=document.getElementById(id);if(!el)return;
  if(el.dataset.bhanuLoaded==='1'){return;}
  el.innerHTML='<p class="loading">Chart load ho raha hai…</p>';
  bhanuFetchJson('/api/intraday?symbol='+encodeURIComponent(symbol)).then(function(p){
    if(!p||p.error||!p.line||!p.line.length){throw new Error(p&&p.error?p.error:'no price data');}
    el.innerHTML='';
    el.dataset.bhanuLoaded='1';
    bhanuLineRender(id,p,symbol);
    if(typeof bhanuMarkLive==='function'){bhanuMarkLive();}
  }).catch(function(e){
    var n=parseInt(el.dataset.bhanuRetries||'0',10)+1;
    el.dataset.bhanuRetries=String(n);
    if(n<=8){
      el.innerHTML='<p class="muted">Chart data load nahi hua — retry ho raha hai… (try '+n+')</p>';
      var delay=n<=2?15000:(n<=4?30000:60000);
      setTimeout(function(){bhanuLazyLine(id,symbol);},delay);
    }else{
      el.innerHTML='<p class="muted">Chart data load nahi ho paya — page refresh karein.</p>';
    }
  });
}
function bhanuFlowRender(id, p){
  // The reference chart is a hand-drawn SVG spaghetti (thin green/red
  // lines, dashed blue NIFTY) — it renders instantly with zero chart
  // library load, exactly like the copy's engine already does. The
  // waterfall tiles on the right rank strongest / weakest.
  var el=document.getElementById(id);if(!el)return false;
  bhanuSvgFlow(el,p.series);
  var up=0,down=0;
  p.series.forEach(function(s){if(s.up){up++;}else{down++;}});
  var leg=document.getElementById(id+'-legend');
  if(leg){leg.textContent=up+' up / '+down+' down — each line is one stock\\'s intraday % change (NIFTY = blue dashed). Failed: '+(p.failed?p.failed.length:0)+'. As of '+p.as_of+'.';}
  var cnt=document.getElementById(id+'-counts');
  if(cnt){cnt.innerHTML='<span class="up">▲ BULLISH <b>'+up+'</b></span><span class="down">▼ BEARISH <b>'+down+'</b></span>';}
  var wf=document.getElementById(id+'-waterfall');
  if(wf){bhanuWaterfall(wf,p.series);}
  return true;
}
function bhanuFlowChart(id){
  var el=document.getElementById(id);if(!el)return;
  el.innerHTML='<p class="muted">Loading flow chart — all stocks…</p>';
  fetch('/api/flow').then(function(r){if(!r.ok){throw new Error('flow '+r.status);}return r.json();}).then(function(p){
    if(!p||!p.series||p.series.length===0){bhanuUnavailable(el,p&&p.error?p.error:'no flow data');return;}
    if(!bhanuFlowRender(id,p)){return;}
    if(typeof bhanuMarkLive==='function'){bhanuMarkLive();}
    window.bhanuRefreshers.push(function(){
      return fetch('/api/flow').then(function(r){if(!r.ok){throw new Error('flow '+r.status);}return r.json();}).then(function(p2){
        if(!p2||!p2.series||p2.series.length===0){throw new Error(p2&&p2.error?p2.error:'no flow data');}
        bhanuFlowRender(id,p2);
      });
    });
  }).catch(function(e){bhanuUnavailable(el,'could not load flow data');});
}
function bhanuSectorRender(id, p){
  // Horizon view: pale green above the NIFTY line, pale pink below it.
  // Dashed blue NIFTY line, bubbles sized by traded value, RUNNING
  // badge on sectors beating NIFTY by >1%. Ranked rows on the right.
  var el=document.getElementById(id);if(!el)return false;
  var W=960,H=620,padL=40,padR=16,padT=16,padB=44;
  var nifty=p.nifty_change===null||p.nifty_change===undefined?0:p.nifty_change;
  var chs=p.sectors.map(function(s){return s.change_pct;}).concat([nifty,0]);
  var rels=p.sectors.map(function(s){return s.rel_pct===null?0:s.rel_pct;}).concat([0]);
  var y0=Math.min.apply(null,chs)-0.2,y1=Math.max.apply(null,chs)+0.2;
  var x0=Math.min.apply(null,rels)-0.2,x1=Math.max.apply(null,rels)+0.2;
  var maxV=Math.max.apply(null,p.sectors.map(function(s){return s.traded_value||1;}));
  function X(v){return padL+(v-x0)/(x1-x0)*(W-padL-padR);}
  function Y(v){return padT+(y1-v)/(y1-y0)*(H-padT-padB);}
  var yn=Y(nifty);
  var svg='<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:auto" role="img" aria-label="Sector map">';
  svg+='<rect x="'+padL+'" y="'+padT+'" width="'+(W-padL-padR)+'" height="'+Math.max(0,yn-padT)+'" fill="#e7f5ec"/>';
  svg+='<rect x="'+padL+'" y="'+yn+'" width="'+(W-padL-padR)+'" height="'+Math.max(0,H-padB-yn)+'" fill="#fbe9ec"/>';
  for(var g=Math.ceil(y0*2)/2;g<=y1;g+=0.5){
    svg+='<line x1="'+padL+'" y1="'+Y(g).toFixed(1)+'" x2="'+(W-padR)+'" y2="'+Y(g).toFixed(1)+'" stroke="#e5e7eb" stroke-width="1"/>';
    svg+='<text x="'+(padL-5)+'" y="'+(Y(g)+3.5).toFixed(1)+'" text-anchor="end" font-size="10" fill="#6b7385">'+(g>0?'+':'')+g.toFixed(1)+'%</text>';
  }
  svg+='<line x1="'+padL+'" y1="'+yn.toFixed(1)+'" x2="'+(W-padR)+'" y2="'+yn.toFixed(1)+'" stroke="#2563eb" stroke-width="1.5" stroke-dasharray="6 4"/>';
  svg+='<text x="'+(W-padR)+'" y="'+(yn-5).toFixed(1)+'" text-anchor="end" font-size="11" font-weight="700" fill="#2563eb">NIFTY '+(nifty>=0?'+':'')+nifty.toFixed(2)+'%</text>';
  p.sectors.forEach(function(s){
    var r=16+40*Math.sqrt((s.traded_value||1)/maxV);
    var cx=X(s.rel_pct===null?0:s.rel_pct),cy=Y(s.change_pct);
    var col=s.up?'#2f9e5f':'#d6455d';
    var top=s.top_member||(s.members&&s.members[0])||'';
    svg+='<a href="/stock?symbol='+encodeURIComponent(top)+'&lang='+bhanuLang()+'" style="cursor:pointer"><title>'+s.name+' — top stock '+top+' — detail page kholo</title>';
    if(s.rel_pct!==null&&s.rel_pct>1){
      svg+='<circle cx="'+cx.toFixed(1)+'" cy="'+cy.toFixed(1)+'" r="'+r.toFixed(1)+'" fill="none" stroke="'+col+'" stroke-width="2"><animate attributeName="r" values="'+r.toFixed(1)+';'+(r+12).toFixed(1)+'" dur="1.6s" repeatCount="indefinite"/><animate attributeName="opacity" values="0.9;0" dur="1.6s" repeatCount="indefinite"/></circle>';
      svg+='<text x="'+cx.toFixed(1)+'" y="'+(cy-r-8).toFixed(1)+'" text-anchor="middle" font-size="9" font-weight="800" fill="'+col+'">RUNNING</text>';
    }
    svg+='<circle cx="'+cx.toFixed(1)+'" cy="'+cy.toFixed(1)+'" r="'+r.toFixed(1)+'" fill="'+col+'" fill-opacity="0.55" stroke="'+col+'" stroke-width="2"/>';
    svg+='<text x="'+cx.toFixed(1)+'" y="'+(cy-4).toFixed(1)+'" text-anchor="middle" font-size="12" font-weight="bold" fill="'+(bhanuDark()?'#fff':'#141a26')+'">'+s.name+'</text>';
    svg+='<text x="'+cx.toFixed(1)+'" y="'+(cy+12).toFixed(1)+'" text-anchor="middle" font-size="11" fill="'+(bhanuDark()?'#fff':'#141a26')+'">'+(s.change_pct>=0?'+':'')+s.change_pct.toFixed(2)+'%</text>';
    svg+='</a>';
  });
  var cats={'TECH & MEDIA':['IT','Telecom'],'FINANCIALS':['Banks','Financials'],'HEALTHCARE':['Pharma'],'CONSUMPTION':['FMCG','Auto'],'COMMODITIES & ENERGY':['Metals','Energy'],'INDUSTRIALS & PSU':['Infra']};
  Object.keys(cats).forEach(function(c){
    var xs=cats[c].map(function(n){var s=p.sectors.filter(function(x){return x.name===n;})[0];return s?(s.rel_pct===null?0:s.rel_pct):null;}).filter(function(v){return v!==null;});
    if(!xs.length){return;}
    var mx=xs.reduce(function(a,b){return a+b;},0)/xs.length;
    svg+='<text x="'+X(mx).toFixed(1)+'" y="'+(H-24)+'" text-anchor="middle" font-size="10" font-weight="700" fill="#6b7385">'+c+'</text>';
  });
  svg+='<text x="'+padL+'" y="'+(H-6)+'" font-size="11" fill="#6b7385">x = sector vs NIFTY (%) — right = beating NIFTY · bubble size = traded value</text>';
  svg+='</svg>';
  el.innerHTML=svg;
  var list=document.getElementById(id+'-list');
  if(list){
    var sorted=p.sectors.slice().sort(function(a,b){return b.change_pct-a.change_pct;});
    var beating=sorted.filter(function(s){return s.rel_pct!==null&&s.rel_pct>0;});
    var html='<h3>Themes beating NIFTY <small>'+beating.length+' of '+p.sectors.length+' sectors</small></h3>';
    sorted.forEach(function(s,i){
      var w=Math.min(100,Math.abs(s.change_pct)*40);
      var top=s.top_member||(s.members&&s.members[0])||'';
      html+='<a class="row" href="/stock?symbol='+encodeURIComponent(top)+'&lang='+bhanuLang()+'"><span class="muted">'+(i+1)+'</span><span>'+s.name+'</span><span><div class="bar" style="width:'+w+'%;background:'+(s.up?'#2f9e5f':'#d6455d')+'"></div></span><span class="'+(s.up?'up':'down')+'">'+(s.change_pct>=0?'+':'')+s.change_pct.toFixed(2)+'%</span></a>';
      if(i===beating.length-1&&beating.length>0&&beating.length<sorted.length){
        html+='<div class="niftysep"><span>NIFTY '+(nifty>=0?'+':'')+nifty.toFixed(2)+'%</span><span>beating ↓ / lagging ↑</span></div>';
      }
    });
    html+='<p class="muted">Bubble size = today\\'s traded value (close × volume). NIFTY change: '+(p.nifty_change===null||p.nifty_change===undefined?'—':p.nifty_change.toFixed(2)+'%')+'. As of '+p.as_of+'.</p>';
    list.innerHTML=html;
  }
  return true;
}
function bhanuSectorMap(id){
  var el=document.getElementById(id);if(!el)return;
  el.innerHTML='<p class="muted">Loading sector map…</p>';
  fetch('/api/sectors').then(function(r){if(!r.ok){throw new Error('sectors '+r.status);}return r.json();}).then(function(p){
    if(!p||!p.sectors||p.sectors.length===0){bhanuUnavailable(el,p&&p.error?p.error:'no sector data');return;}
    bhanuSectorRender(id,p);
    if(typeof bhanuMarkLive==='function'){bhanuMarkLive();}
    window.bhanuRefreshers.push(function(){
      return fetch('/api/sectors').then(function(r){if(!r.ok){throw new Error('sectors '+r.status);}return r.json();}).then(function(p2){
        if(!p2||!p2.sectors||p2.sectors.length===0){throw new Error(p2&&p2.error?p2.error:'no sector data');}
        bhanuSectorRender(id,p2);
      });
    });
  }).catch(function(e){bhanuUnavailable(el,'could not load sector data');});
}
function bhanuCandleBoxes(wrap, boxes){
  var out=[];
  (boxes||[]).forEach(function(b){
    var d=document.createElement('div');
    d.style.cssText='position:absolute;pointer-events:none;border:1px dashed '+b.border+
      ';background:'+b.color+';font-size:10px;color:'+b.border+';padding:1px 4px;overflow:hidden;white-space:nowrap;';
    d.textContent=b.label;d.style.display='none';
    wrap.appendChild(d);
    out.push({spec:b,el:d});
  });
  return out;
}
function bhanuCandleLayout(st){
  var w=st.el.clientWidth||900;
  st.boxes.forEach(function(b){
    var s=b.spec;
    var x1=st.chart.timeScale().timeToCoordinate(s.from);
    var x2=s.to?st.chart.timeScale().timeToCoordinate(s.to):w;
    var y1=st.series.priceToCoordinate(s.top),y2=st.series.priceToCoordinate(s.bottom);
    if(b.hidden||x1===null||y1===null||y2===null){b.el.style.display='none';return;}
    if(x2===null){x2=w;}
    b.el.style.display='block';
    b.el.style.left=x1+'px';b.el.style.width=Math.max(0,x2-x1)+'px';
    b.el.style.top=Math.min(y1,y2)+'px';b.el.style.height=Math.max(0,Math.abs(y2-y1))+'px';
  });
}
function bhanuIndicators(id,st){
  const p=st.payload, candles=p.candles;
  const ema=[];
  if(candles.length>=20){
    let value=candles.slice(0,20).reduce((sum,c)=>sum+c.close,0)/20;
    ema.push({time:candles[19].time,value:value});
    for(const c of candles.slice(20)){value+=(2/21)*(c.close-value);ema.push({time:c.time,value:value});}
  }
  const vol=p.volume||[];
  if(!st.ema)st.ema=st.chart.addLineSeries({color:'#c18013',lineWidth:2,priceLineVisible:false,lastValueVisible:false});
  st.ema.setData(ema);st.ema.applyOptions({visible:st.overlays.ema!==false});
  if(!st.hist){
    st.hist=st.chart.addHistogramSeries({priceScaleId:'volume',priceFormat:{type:'volume'},color:'#5793ad',priceLineVisible:false,lastValueVisible:false});
    st.hist.priceScale().applyOptions({scaleMargins:{top:0.82,bottom:0}});
  }
  st.hist.setData(vol);st.hist.applyOptions({visible:st.overlays.volume!==false});
  const label=document.getElementById(id+'-indicators');
  const last=candles[candles.length-1];
  const barTime=new Date(last.time*1000).toLocaleString('en-IN',{timeZone:'Asia/Kolkata',hour12:false});
  if(label)label.textContent=['Last bar '+barTime+' IST',ema.length?'EMA 20':'EMA 20 needs 20 bars',p.vwap&&p.vwap.length?'VWAP':'VWAP unavailable',vol.length?'Reported volume':'Volume unavailable'].join(' · ');
}
function bhanuCandleApplyOverlays(id){
  // Rebuild every overlay from st.payload + st.overlays (the toggle
  // state). Price lines are never toggled — entry/SL/TP/levels always
  // show — but each line's axis label follows its label_visible flag
  // (the server hides the label of the lower-priority line when two
  // sit close enough to overlap, e.g. PDL vs TP).
  var st=(window.bhanuCandles||{})[id];
  if(!st||!st.payload){return;}
  bhanuIndicators(id,st);
  var p=st.payload, ov=st.overlays||{};
  var show=function(k){return ov[k]!==false;};
  (st.priceLines||[]).forEach(function(h){try{st.series.removePriceLine(h);}catch(e){}});
  st.priceLines=[];
  (p.lines||[]).forEach(function(ln){
    st.priceLines.push(st.series.createPriceLine({price:ln.price,color:ln.color,lineWidth:1,
      lineStyle:ln.style||2,axisLabelVisible:ln.label_visible!==false,
      title:ln.label_visible===false?'':ln.label}));
  });
  if(show('vwap')&&p.vwap&&p.vwap.length){
    if(st.vwap){st.vwap.setData(p.vwap);}
    else{st.vwap=st.chart.addLineSeries({color:'#7e57c2',lineWidth:1,priceLineVisible:false,lastValueVisible:false});st.vwap.setData(p.vwap);}
  }else if(st.vwap){try{st.chart.removeSeries(st.vwap);}catch(e){} st.vwap=null;}
  var mk=(p.markers||[]).filter(function(m){
    var g=m.group||'structure';
    return g==='trades'?show('trades'):show('structure');
  });
  st.series.setMarkers(mk);
  st.boxes.forEach(function(b){
    var k=b.spec.kind||'';
    b.hidden=(k==='ob'&&!show('orderblock'))||(k==='fvg'&&!show('fvg'));
  });
  bhanuCandleLayout(st);
}
function bhanuToggleOverlay(chartId, key, btn){
  var st=(window.bhanuCandles||{})[chartId];if(!st){return;}
  var avail=st.payload&&st.payload.overlays?st.payload.overlays[key]:true;
  if(avail===false){return;}
  st.overlays[key]=!st.overlays[key];
  if(btn){btn.classList.toggle('on',!!st.overlays[key]);}
  bhanuCandleApplyOverlays(chartId);
}
function bhanuStockLoad(symbol, interval){
  var iv=interval||'5m';
  var host=document.getElementById('stock-body');if(!host){return;}
  const request=Symbol();host.activeRequest=request;
  document.querySelectorAll('#stock-tf .tfbtn').forEach(function(b){
    b.classList.toggle('on',b.dataset.iv===iv);
  });
  host.stockUrl='/api/stock?symbol='+encodeURIComponent(symbol)+'&interval='+encodeURIComponent(iv);
  bhanuDetail(host.stockUrl).then(function(r){
    if(!r.ok){throw new Error('stock '+r.status);}return r.json();
  }).then(function(d){
    if(host.activeRequest!==request)return;
    if(!d||d.error){throw new Error(d&&d.error?d.error:'no stock data');}
    var hd=document.getElementById('stock-head');
    if(hd){hd.innerHTML=d.header_html||'<p class="muted">Header unavailable.</p>';}
    var sc=document.getElementById('stock-setup');
    if(sc){sc.innerHTML=d.setup_html||'<p class="muted">Setup unavailable.</p>';}
    var sm=document.getElementById('stock-summary');
    if(sm){sm.innerHTML=d.summary_html||'';}
    var ov=d.chart&&d.chart.overlays?d.chart.overlays:{};
    document.querySelectorAll('#stock-pills .opill').forEach(function(b){
      var k=b.dataset.ov;
      var avail=ov[k]!==false;
      if(k==='volume')avail=Boolean(d.chart&&d.chart.volume&&d.chart.volume.length);
      if(k==='ema')avail=Boolean(d.chart&&d.chart.candles&&d.chart.candles.length>=20);
      b.disabled=!avail;
      if(!avail){b.classList.remove('on');}
      else{b.classList.add('on');}
    });
    var st=(window.bhanuCandles||{})['stock-chart'];
    if(st&&st.chart){window.removeEventListener('resize',st.onResize);st.boxes.forEach(b=>b.el.remove());try{st.chart.remove();}catch(e){} delete window.bhanuCandles['stock-chart'];}
    var el=document.getElementById('stock-chart');
    if(el){el.innerHTML='';}
    if(d.chart&&d.chart.candles&&d.chart.candles.length){
      bhanuCandleRender('stock-chart',d.chart,null);
      if(!host.refreshRegistered){
        host.refreshRegistered=true;
        window.bhanuRefreshers.push(async function(){
          const active=host.activeRequest;
          try{
            const response=await bhanuDetail(host.stockUrl);
            const fresh=await response.json();
            if(active!==host.activeRequest)return;
            if(!response.ok||fresh.error||!fresh.chart||fresh.chart.error)
              throw Error(fresh.error||(fresh.chart&&fresh.chart.error)||'Chart unavailable');
            if(window.bhanuCandles['stock-chart'])bhanuCandleUpdate('stock-chart',fresh.chart);
            if(fresh.header_html)hd.innerHTML=fresh.header_html;
            if(fresh.setup_html)sc.innerHTML=fresh.setup_html;
          }catch(error){
            if(active===host.activeRequest){
              const legend=document.getElementById('stock-chart-indicators');
              if(legend)legend.textContent='Refresh unavailable · last received chart · '+error.message;
            }
            throw error;
          }
        });
      }
    }else if(el){
      bhanuUnavailable(el,d.chart&&d.chart.error?d.chart.error:'no chart data');
    }
    if(typeof bhanuMarkLive==='function'){bhanuMarkLive();}
  }).catch(function(e){
    if(host.activeRequest!==request)return;
    host.innerHTML='<p class="muted">Stock data load nahi ho paya — page refresh karein.</p>';
  });
}
function bhanuCandleUpdate(id, p){
  var st=(window.bhanuCandles||{})[id];
  if(!st){throw new Error('no candle chart');}
  if(!p||p.error||!p.candles||!p.candles.length){throw new Error(p&&p.error?p.error:'no candle data');}
  st.series.setData(p.candles);
  st.payload=p;
  st.boxes.forEach(function(b){if(b.el.parentNode){b.el.parentNode.removeChild(b.el);}});
  st.boxes=bhanuCandleBoxes(st.wrap,p.boxes||[]);
  bhanuCandleApplyOverlays(id);
}
function bhanuCandleRender(id, p, refreshUrl){
  var wrap=document.getElementById(id+'-wrap');var el=document.getElementById(id);
  if(!wrap||!el)return;
  if(!p||!p.candles||p.candles.length===0){bhanuUnavailable(el,p&&p.error?p.error:'no price data');return;}
  if(typeof LightweightCharts==='undefined'){
    bhanuSvgCandles(el,p.candles);
    var legend=document.getElementById(id+'-indicators');
    if(legend)legend.textContent='Basic candle view · interactive indicators unavailable';
    if(refreshUrl){
      window.bhanuRefreshers.push(function(){
        return bhanuDetail(refreshUrl).then(function(r){
          if(!r.ok){throw new Error('candles '+r.status);}return r.json();
        }).then(function(p2){
          if(!p2||p2.error||!p2.candles||!p2.candles.length){throw new Error(p2&&p2.error?p2.error:'no candle data');}
          bhanuSvgCandles(el,p2.candles);
        });
      });
    }
    return;
  }
  var o=bhanuChartOpts(420);o.width=el.clientWidth||900;
  var ch=LightweightCharts.createChart(el,o);
  var cs=ch.addCandlestickSeries({upColor:'#2f9e5f',downColor:'#d6455d',
    wickUpColor:'#2f9e5f',wickDownColor:'#d6455d',borderVisible:false});
  cs.setData(p.candles);
  var vw=null;
  if(p.vwap&&p.vwap.length){
    vw=ch.addLineSeries({color:'#7e57c2',lineWidth:1,priceLineVisible:false,lastValueVisible:false});
    vw.setData(p.vwap);
  }
  var st={chart:ch,series:cs,vwap:vw,priceLines:[],boxes:[],wrap:wrap,el:el,payload:p,
    overlays:{orderblock:true,fvg:true,structure:true,liquidity:false,vwap:true,ema:true,volume:true,trades:true}};
  window.bhanuCandles[id]=st;
  st.boxes=bhanuCandleBoxes(wrap,p.boxes||[]);
  bhanuCandleApplyOverlays(id);
  function layout(){bhanuCandleLayout(st);}
  ch.timeScale().subscribeVisibleTimeRangeChange(layout);
  ch.timeScale().subscribeVisibleLogicalRangeChange(layout);
  st.onResize=function(){ch.applyOptions({width:el.clientWidth||900});layout();};
  window.addEventListener('resize',st.onResize);
  if(refreshUrl){
    window.bhanuRefreshers.push(function(){
      return bhanuDetail(refreshUrl).then(function(r){
        if(!r.ok){throw new Error('candles '+r.status);}return r.json();
      }).then(function(p2){bhanuCandleUpdate(id,p2);});
    });
  }
  ch.timeScale().fitContent();layout();
}
function bhanuCandleChart(id, refreshUrl){
  var el=document.getElementById(id);if(!el)return;
  var raw=document.getElementById(id+'-data');if(!raw){bhanuUnavailable(el,'no chart data');return;}
  var p;try{p=JSON.parse(raw.textContent);}catch(e){bhanuUnavailable(el,'bad chart data');return;}
  bhanuCandleRender(id,p,refreshUrl);
}
function bhanuLazyCandle(id, url){
  var el=document.getElementById(id);if(!el)return;
  if(el.dataset.bhanuLoaded==='1'){return;}
  el.innerHTML='<p class="loading">Chart load ho raha hai…</p>';
  bhanuDetail(url).then(function(r){
    if(!r.ok){throw new Error('candles '+r.status);}return r.json();
  }).then(function(p){
    if(!p||p.error||!p.candles||!p.candles.length){throw new Error(p&&p.error?p.error:'no price data');}
    el.innerHTML='';
    el.dataset.bhanuLoaded='1';
    bhanuCandleRender(id,p,url);
    if(typeof bhanuMarkLive==='function'){bhanuMarkLive();}
  }).catch(function(e){
    var n=parseInt(el.dataset.bhanuRetries||'0',10)+1;
    el.dataset.bhanuRetries=String(n);
    if(n<=8){
      el.innerHTML='<p class="muted">Chart data load nahi hua — retry ho raha hai… (try '+n+')</p>';
      var delay=n<=2?15000:(n<=4?30000:60000);
      setTimeout(function(){bhanuLazyCandle(id,url);},delay);
    }else{
      el.innerHTML='<p class="muted">Chart data load nahi ho paya — page refresh karein.</p>';
    }
  });
}
function bhanuQuoteFetch(){
  return fetch('/api/quotes').then(function(r){
    if(!r.ok){throw new Error('quotes '+r.status);}return r.json();
  }).then(function(p){
    if(!p||!p.quotes||!p.quotes.length){throw new Error('no quote data');}
    p.quotes.forEach(function(q){
      var slug=String(q.symbol).replace(/[^A-Za-z0-9]/g,'').toLowerCase();
      var pe=document.getElementById('q-'+slug+'-price');
      var ce=document.getElementById('q-'+slug+'-chg');
      var ve=document.getElementById('q-'+slug+'-prev');
      const state=document.getElementById('q-'+slug+'-status');
      if(state)state.textContent=q.error||'Last reported · source time unverified';
      if(q.price===null||q.price===undefined){
        if(pe)pe.textContent='data unavailable';
        if(ce)ce.textContent='—';
        if(ve)ve.textContent='prev close —';
        return;
      }
      if(ce&&q.change_pct==null)ce.textContent='—';
      if(ve&&q.prev_close==null)ve.textContent='prev close —';
      if(pe){pe.textContent=Number(q.price).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});}
      if(ce&&q.change_pct!==null&&q.change_pct!==undefined){
        ce.textContent=(q.change_pct>=0?'+':'')+Number(q.change_pct).toFixed(2)+'%';
        ce.className='index-change '+(q.change_pct>=0?'up':'down');
      }
      if(ve&&q.prev_close!==null&&q.prev_close!==undefined){ve.textContent='prev close '+Number(q.prev_close).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});}
      var card=document.getElementById('q-'+slug+'-card');
      if(card&&q.change_pct!==null&&q.change_pct!==undefined){card.classList.toggle('neg',q.change_pct<0);}
    });
    if(typeof bhanuMarkLive==='function'){bhanuMarkLive();}
  }).catch(function(error){
    document.querySelectorAll('.quote-status').forEach(el=>{
      el.textContent='Refresh unavailable · last received data only';
    });
    throw error;
  });
}
function bhanuQuoteRefresh(){
  bhanuQuoteFetch().catch(function(e){
    ['nsei','nsebank','bsesn'].forEach(function(slug){
      var pe=document.getElementById('q-'+slug+'-price');
      if(pe){pe.textContent='data unavailable';}
    });
  });
  // Quotes tick every 2 seconds too — the price on the card moves
  // while you watch, it does not wait for the 60-second cycle.
  if(!window.bhanuQuoteTimer){
    window.bhanuQuoteTimer=setInterval(function(){
      bhanuQuoteFetch().catch(function(){});
    },2000);
  }
  window.bhanuRefreshers.push(function(){return bhanuQuoteFetch();});
}
function bhanuNewsFetch(lang){
  return fetch('/api/news?lang='+encodeURIComponent(lang||'hi')).then(function(r){
    if(!r.ok){throw new Error('news '+r.status);}return r.json();
  }).then(function(p){
    if(!p||!p.html){throw new Error(p&&p.error?p.error:'no news data');}
    var wrap=document.getElementById('news-feed');
    if(wrap){wrap.innerHTML=p.html;}
    if(typeof bhanuMarkLive==='function'){bhanuMarkLive();}
  });
}
function bhanuNewsRefresh(lang){
  var wrap=document.getElementById('news-feed');
  if(!wrap){return;}
  bhanuNewsFetch(lang).catch(function(e){
    wrap.innerHTML='<p class="muted">News load nahi ho paya — page refresh karein.</p>';
  });
  window.bhanuRefreshers.push(function(){return bhanuNewsFetch(lang);});
}
function bhanuMCHistHTML(hist){
  if(!hist||!hist.buckets||!hist.buckets.length||!hist.n){return "<p class='muted'>Distribution unavailable — no replay outcomes.</p>";}
  var max=0;hist.buckets.forEach(function(b){if(b.count>max){max=b.count;}});
  if(!max){max=1;}
  var rows='';
  hist.buckets.forEach(function(b){
    var w=Math.max(2,100*b.count/max);
    var color=b.label.indexOf('Target')===0?'#2f9e5f':(b.label.indexOf('Stop')===0?'#d6455d':'#2563eb');
    rows+='<tr><td>'+b.label+'</td><td style="width:55%"><div style="height:18px;border-radius:4px;background:'+color+';width:'+w.toFixed(1)+'%"></div></td><td>'+b.count.toLocaleString('en-IN')+' ('+b.pct.toFixed(1)+'%)</td></tr>';
  });
  var avg='';
  if(hist.avg_r!==null&&hist.avg_r!==undefined){avg='<p>Average outcome across all '+hist.n.toLocaleString('en-IN')+' replays: <b>'+(hist.avg_r>=0?'+':'')+hist.avg_r.toFixed(2)+'R</b> (the bar the average sits in is the typical replay).</p>';}
  return '<h3>Monte Carlo distribution — '+hist.n.toLocaleString('en-IN')+' replays</h3><p class="muted">Each replay ran the setup on resampled 5m bars from the last 60 days. This is how the 10,000 checks split up:</p><div class="table-wrap"><table>'+rows+'</table></div>'+avg;
}
function bhanuMCRender(mc){
  var hist=document.getElementById('mc-hist');
  if(hist&&mc.histogram){hist.innerHTML=bhanuMCHistHTML(mc.histogram);}
  function set(id,txt){var e=document.getElementById(id);if(e){e.textContent=txt;}}
  set('mc-title','Monte Carlo Time Machine — '+Number(mc.n).toLocaleString('en-IN')+' replays');
  set('mc-setup',mc.symbol+' '+(mc.direction==='short'?'PUT':mc.direction==='long'?'CALL':mc.direction)+' entry '+mc.entry+', stop '+mc.stop+', target '+mc.target+'.');
  set('mc-target-pct',Number(mc.target_first_pct).toFixed(1)+'%');
  set('mc-stop-pct',Number(mc.stop_first_pct).toFixed(1)+'%');
  set('mc-neither-pct',Number(mc.neither_pct).toFixed(1)+'%');
  set('mc-summary','Average outcome: '+(mc.avg_r>=0?'+':'')+Number(mc.avg_r).toFixed(2)+'R. Best case (target hit): +'+Number(mc.best_case_r).toFixed(1)+'R. Worst case (stop hit): '+Number(mc.worst_case_r).toFixed(1)+'R. Verdict: '+mc.verdict+' (break-even '+Number(mc.break_even_pct).toFixed(1)+'% target-first among decisive replays). Reality check only — resampled bars cannot see the future, and fees/slippage are ignored.');
}
function bhanuMCRefresh(){
  var card=document.getElementById('mc-card');
  if(!card){return;}
  window.bhanuRefreshers.push(function(){
    return fetch('/api/timemachine').then(function(r){
      if(!r.ok){throw new Error('timemachine '+r.status);}return r.json();
    }).then(function(d){
      if(!d||d.error||!d.monte_carlo||!d.monte_carlo.available){throw new Error(d&&d.error?d.error:'no Monte Carlo data');}
      bhanuMCRender(d.monte_carlo);
    });
  });
}
function bhanuOrbHitsFetch(){
  var el=document.getElementById('orb-hits');
  if(!el){return;}
  var load=function(){
    return fetch('/api/orb?limit=10').then(function(r){
      if(!r.ok){throw new Error('orb '+r.status);}return r.json();
    }).then(function(d){
      if(!d||!d.html){throw new Error(d&&d.error?d.error:'no scan data');}
      el.innerHTML=d.html;
      if(typeof bhanuMarkLive==='function'){bhanuMarkLive();}
    });
  };
  load().catch(function(e){el.innerHTML='<p class="muted">ORB scan load nahi ho paya — page refresh karein.</p>';});
  window.bhanuRefreshers.push(load);
}
function bhanuRobotFetch(){
  var el=document.getElementById('robot-status');
  if(!el){return;}
  var load=function(){
    return fetch('/api/robot').then(function(r){
      if(!r.ok){throw new Error('robot '+r.status);}return r.json();
    }).then(function(d){
      if(!d||!d.html){throw new Error(d&&d.error?d.error:'no robot data');}
      el.innerHTML=d.html;
      if(typeof bhanuMarkLive==='function'){bhanuMarkLive();}
    });
  };
  load().catch(function(e){el.innerHTML='<p class="muted">Robot status load nahi ho paya — page refresh karein.</p>';});
  window.bhanuRefreshers.push(load);
}
function bhanuLevelsMiniFetch(){
  var el=document.getElementById('levels-mini');
  if(!el){return;}
  var load=function(){
    return fetch('/api/levelsmini').then(function(r){
      if(!r.ok){throw new Error('levelsmini '+r.status);}return r.json();
    }).then(function(d){
      if(!d||!d.html){throw new Error(d&&d.error?d.error:'no levels data');}
      el.innerHTML=d.html;
      if(typeof bhanuMarkLive==='function'){bhanuMarkLive();}
    });
  };
  load().catch(function(e){el.innerHTML='<p class="muted">Levels load nahi ho paye — page refresh karein.</p>';});
  window.bhanuRefreshers.push(load);
}
function bhanuTmMiniFetch(){
  var el=document.getElementById('tm-mini');
  if(!el){return;}
  var load=function(){
    return fetch('/api/tmmini').then(function(r){
      if(!r.ok){throw new Error('tmmini '+r.status);}return r.json();
    }).then(function(d){
      if(!d||!d.html){throw new Error(d&&d.error?d.error:'no time machine data');}
      el.innerHTML=d.html;
      if(typeof bhanuMarkLive==='function'){bhanuMarkLive();}
    });
  };
  load().catch(function(e){el.innerHTML='<p class="muted">Time Machine load nahi ho paya — page refresh karein.</p>';});
  window.bhanuRefreshers.push(load);
}
function bhanuOrbBreakdown(sym){
  var bd=document.getElementById('orb-breakdown');
  if(!bd||!sym){return;}
  bd.innerHTML='<p class="loading">Signal breakdown load ho raha hai…</p>';
  bhanuDetail('/api/orbbreakdown?symbol='+encodeURIComponent(sym)).then(function(r){
    if(!r.ok){throw new Error('breakdown '+r.status);}return r.json();
  }).then(function(d){
    if(d&&d.html){bd.innerHTML=d.html;}
    else{bd.innerHTML='<p class="muted">Signal breakdown unavailable.</p>';}
  }).catch(function(e){bd.innerHTML='<p class="muted">Signal breakdown load nahi ho paya.</p>';});
}
function bhanuOrbLoad(querySymbol){
  var table=document.getElementById('orb-table');
  var symEl=document.getElementById('orb-chart-symbol');
  var pick=function(sym){
    if(!sym){return;}
    if(symEl){symEl.textContent='Chart symbol: '+sym+' — 5m candles, VWAP, order blocks, S/R.';}
    bhanuLazyCandle('orb-chart','/api/orbchart?symbol='+encodeURIComponent(sym));
    bhanuOrbBreakdown(sym);
  };
  if(querySymbol){pick(querySymbol);}
  if(table){
    fetch('/api/orb').then(function(r){if(!r.ok){throw new Error('orb '+r.status);}return r.json();}).then(function(d){
      if(!d||!d.html){throw new Error(d&&d.error?d.error:'no scan data');}
      table.innerHTML=d.html;
      if(typeof bhanuMarkLive==='function'){bhanuMarkLive();}
      if(!querySymbol){pick(d.top_symbol);}
    }).catch(function(e){table.innerHTML='<p class="muted">ORB scan load nahi ho paya — page refresh karein.</p>';});
  }
}
function bhanuLevelsBreakdown(sym){
  var bd=document.getElementById('levels-breakdown');
  if(!bd||!sym){return;}
  bd.innerHTML='<p class="loading">Signal breakdown load ho raha hai…</p>';
  bhanuDetail('/api/levelsbreakdown?symbol='+encodeURIComponent(sym)).then(function(r){
    if(!r.ok){throw new Error('breakdown '+r.status);}return r.json();
  }).then(function(d){
    if(d&&d.html){bd.innerHTML=d.html;}
    else{bd.innerHTML='<p class="muted">Signal breakdown unavailable.</p>';}
  }).catch(function(e){bd.innerHTML='<p class="muted">Signal breakdown load nahi ho paya.</p>';});
}
function bhanuLevelsLoad(querySymbol){
  var table=document.getElementById('levels-table');
  var symEl=document.getElementById('levels-chart-symbol');
  var pick=function(sym){
    if(!sym){return;}
    if(symEl){symEl.textContent='Chart symbol: '+sym+' — 5m candles, VWAP, order blocks, S/R.';}
    bhanuLazyCandle('levels-chart','/api/levelschart?symbol='+encodeURIComponent(sym));
    bhanuLevelsBreakdown(sym);
  };
  if(querySymbol){pick(querySymbol);}
  if(table){
    fetch('/api/levels').then(function(r){if(!r.ok){throw new Error('levels '+r.status);}return r.json();}).then(function(d){
      if(!d||!d.html){throw new Error(d&&d.error?d.error:'no levels data');}
      table.innerHTML=d.html;
      if(typeof bhanuMarkLive==='function'){bhanuMarkLive();}
      if(!querySymbol){pick(d.top_symbol);}
    }).catch(function(e){table.innerHTML='<p class="muted">Levels load nahi ho paye — page refresh karein.</p>';});
  }
}
function bhanuTimemachineLoad(){
  var el=document.getElementById('timemachine-body');
  if(!el){return;}
  fetch('/api/timemachine').then(function(r){if(!r.ok){throw new Error('timemachine '+r.status);}return r.json();}).then(function(d){
    if(!d||!d.html){throw new Error(d&&d.error?d.error:'no time machine data');}
    el.innerHTML=d.html;
    if(typeof bhanuMarkLive==='function'){bhanuMarkLive();}
    bhanuMCRefresh();
  }).catch(function(e){el.innerHTML='<p class=\"muted\">Time Machine load nahi ho paya — page refresh karein.</p>';});
}
function bhanuTmCheckSubmit(ev){
  if(ev){ev.preventDefault();}
  var f=document.getElementById('tm-check-form');
  var out=document.getElementById('tm-check-result');
  if(!f||!out){return false;}
  var q='symbol='+encodeURIComponent(f.symbol.value)
    +'&entry='+encodeURIComponent(f.entry.value)
    +'&stop='+encodeURIComponent(f.stop.value)
    +'&target='+encodeURIComponent(f.target.value)
    +'&direction='+encodeURIComponent(f.direction.value);
  out.innerHTML='<p class="loading">10,000 replays chal rahe hain…</p>';
  bhanuDetail('/api/timemachine-check?'+q).then(function(r){if(!r.ok){throw new Error('check '+r.status);}return r.json();}).then(function(d){
    if(d&&d.html){out.innerHTML=d.html;}
    else{out.innerHTML='<p class="muted">Check nahi ho paya — '+((d&&d.error)?d.error:'dobara try karein')+'.</p>';}
    if(typeof bhanuMarkLive==='function'){bhanuMarkLive();}
  }).catch(function(e){out.innerHTML='<p class="muted">Check nahi ho paya — page refresh karke dobara try karein.</p>';});
  return false;
}
function bhanuSkillsLoad(){
  var el=document.getElementById('skills-table');
  if(!el){return;}
  bhanuDetail('/api/skills').then(function(r){if(!r.ok){throw new Error('skills '+r.status);}return r.json();}).then(function(d){
    if(!d||!d.html){throw new Error(d&&d.error?d.error:'no skills data');}
    el.innerHTML=d.html;
    if(typeof bhanuMarkLive==='function'){bhanuMarkLive();}
  }).catch(function(e){el.innerHTML='<p class="muted">Skills load nahi ho paye — page refresh karein.</p>';});
}
</script>"""


def _line_chart_block(chart_id: str, label: str, yahoo_symbol: str,
                      height: int = 260) -> str:
    """A line-chart card, loaded lazily: the page renders instantly
    with a "loading" state and ``bhanuLazyLine`` fetches the data from
    /api/intraday — no Yahoo fetch ever blocks a page render.

    NOTE: the card carries NO inline <script>. The ``bhanuLazyLine``
    call lives in the page's scripts section, AFTER ``_chart_js()``
    defines it. An inline call here used to execute during parsing,
    before the function existed — a ReferenceError, so the fetch never
    started and the card sat on "Chart load ho raha hai…" forever
    while the rest of the page worked (that exact bug left the 3 home
    intraday charts blank)."""
    cached_tick = (poller_mod.get_spark(yahoo_symbol) or {}).get("time") or 0
    return (
        f"<div class='card'><h3>{_esc(label)} — intraday</h3>"
        f"<p class='feed-stamp' id='{chart_id}-stamp' data-tick='{_esc(cached_tick)}'>2 s poll · source time unavailable</p>"
        f"<p class='cache-note' id='{chart_id}-cache-note'></p>"
        f"<div id='{chart_id}' class='chart-box' style='height:{height}px'>"
        f"<p class='loading'>Chart load ho raha hai…</p></div></div>"
    )


def _candle_chart_block(chart_id: str, symbol: str, note: str | None = None) -> str:
    """A candlestick + overlays card, loaded lazily: the page renders
    instantly with a "loading" state and the page loader
    (``bhanuOrbLoad`` / ``bhanuLevelsLoad`` / ``bhanuStockLoad``)
    fetches the data from /api/orbchart, /api/levelschart or
    /api/stock — no Yahoo fetch ever blocks a page render. ``symbol``
    is the chart symbol when known from the URL, else '' (the loader
    fills it in from the scan)."""
    heading = (
        f"{_esc(symbol)} — candlestick with overlays"
        if symbol else "Candlestick with overlays"
    )
    note_html = note or (
        "Candles: 5m. Purple line = VWAP. Dashed lines = ORB range / PDH / PDL / "
        "support / resistance. Green box = target zone, red box = stop zone, "
        "pale boxes = order blocks / fair value gaps. Arrows = CHoCH / BOS breaks."
    )
    return (
        f"<div class='card'><h3>{heading}</h3>"
        f"<p class='muted'>{note_html}</p><p class='feed-stamp' id='{chart_id}-indicators'>Indicators awaiting candles</p>"
        f"<div id='{chart_id}-wrap' class='chart-wrap'>"
        f"<div id='{chart_id}' class='chart-box tall'>"
        f"<p class='loading'>Chart load ho raha hai…</p></div></div></div>"
    )


def _mc_histogram_svg(hist: dict) -> str:
    """Monte Carlo distribution as server-side SVG bars (works offline).

    Five R buckets with counts + % labels, and a marker for the average R.
    """
    buckets = hist.get("buckets", [])
    n = hist.get("n", 0)
    avg_r = hist.get("avg_r")
    if not buckets or n == 0:
        return "<p class='muted'>Distribution unavailable — no replay outcomes.</p>"
    max_count = max(b["count"] for b in buckets) or 1
    rows = ""
    for b in buckets:
        w = max(2, 100.0 * b["count"] / max_count)
        color = "#2f9e5f" if b["label"].startswith("Target") else (
            "#d6455d" if b["label"].startswith("Stop") else "#2563eb"
        )
        rows += (
            f"<tr><td>{_esc(b['label'])}</td>"
            f"<td style='width:55%'><div style='height:18px;border-radius:4px;"
            f"background:{color};width:{w:.1f}%'></div></td>"
            f"<td>{b['count']:,} ({b['pct']:.1f}%)</td></tr>"
        )
    avg_line = ""
    if avg_r is not None:
        avg_line = (
            f"<p>Average outcome across all {n:,} replays: "
            f"<b>{avg_r:+.2f}R</b> (the bar the average sits in is the "
            f"typical replay).</p>"
        )
    return (
        f"<h3>Monte Carlo distribution — {n:,} replays</h3>"
        f"<p class='muted'>Each replay ran today's setup on resampled 5m bars "
        f"from the last 60 days. This is how the 10,000 checks split up:</p>"
        f"<div class='table-wrap'><table>{rows}</table></div>{avg_line}"
    )


def _index_cards_html(lang: str = "hi") -> str:
    """Three index cards (NIFTY / BANK NIFTY / SENSEX) in the reference
    tile style: 76px tile, sparkline on top, label overlay, price +
    change in the foot. Filled in lazily by ``bhanuQuoteFetch``
    (/api/quotes) and the 5s live-line ticks, which also draw each
    card's sparkline from the same /api/live line. Cell ids:
    ``q-<slug>-price`` / ``q-<slug>-chg`` / ``q-<slug>-prev`` /
    ``q-<slug>-spark`` / ``q-<slug>-card``. Each card links to that
    index's /stock detail page."""
    cards = ""
    for _symbol, label, slug, name in (
        ("^NSEI", "NIFTY 50", "nsei", "NIFTY"),
        ("^NSEBANK", "BANK NIFTY", "nsebank", "BANKNIFTY"),
        ("^BSESN", "SENSEX", "bsesn", "SENSEX"),
    ):
        cards += (
            f"<a class='icard' id='q-{slug}-card' href='/stock?symbol={name}&lang={lang}'>"
            f"<div class='spark' id='q-{slug}-spark'></div>"
            f"<div class='lab'><b>{_esc(label)}</b></div>"
            f"<small class='quote-status' id='q-{slug}-status'>Waiting for source data</small>"
            f"<div class='foot'><b id='q-{slug}-price'>loading…</b>"
            f"<span id='q-{slug}-chg'>—</span>"
            f"<span class='muted' id='q-{slug}-prev'>prev close —</span></div></a>"
        )
    return f"<div class='index-cards'>{cards}</div>"


def _news_feed_html(lang: str, news: dict | None = None) -> str:
    """Prominent news feed section (automatic, real-time).

    Each headline is a card row with source + time and a "Real-time
    check" label. Used on home and /skills. A failed fetch shows an
    honest "unavailable" — never an empty or fake feed. When the live
    engine has a warmed news check, pass it in (``news``) so the page
    never waits on a fresh fetch.
    """
    t = lambda k: i18n_mod.t(k, lang)
    if news is None and poller_mod.is_running():
        return f"<div class='card'><h3>{t('news_feed')}</h3><p class='loading'>News feed warming up…</p></div>"
    if news is None:
        try:
            news = _cached("news:NIFTY", 300, lambda: news_mod.check_symbol("NIFTY"))
        except Exception as exc:  # noqa: BLE001
            return (
                f"<div class='card'><h3>{t('news_feed')}</h3>"
                f"<p class='muted'>Real-time check — News unavailable ({_esc(exc)})</p></div>"
            )
    latest = news.get("latest", [])[:8]
    sources = news.get("sources", {}) or {}
    # The Marketaux note already starts with "Marketaux: ..." — strip
    # that prefix so the line does not read "Marketaux: Marketaux: ...".
    mx_status = sources.get('marketaux', 'unavailable')
    if mx_status.startswith("Marketaux: "):
        mx_status = mx_status[len("Marketaux: "):]
    source_line = (
        f"Real-time check — NSE announcements: "
        f"{sources.get('nse_announcements', 'unavailable')}; "
        f"Yahoo headlines: {sources.get('yahoo_headlines', 'unavailable')}; "
        f"Marketaux: {mx_status}."
    )
    if not latest:
        return (
            f"<div class='card'><h3>{t('news_feed')}</h3>"
            f"<p class='muted'>{_esc(source_line)}</p>"
            f"<p>News unavailable right now — no headlines returned.</p></div>"
        )
    items = "".join(
        f"<div class='news-item'><b>{_esc(it.get('title', ''))}</b><br>"
        f"<span class='muted'>{_esc(it.get('source', ''))}"
        + (f" · {_esc(it.get('published', ''))}" if it.get("published") else "")
        + (f" · {', '.join(it.get('tags', []))}" if it.get("tags") else "")
        + (f" · sentiment {it['sentiment']:+.2f}"
           if isinstance(it.get("sentiment"), (int, float)) else "")
        + "</span></div>"
        for it in latest
    )
    fresh = news.get("items", []) or []
    fresh_line = (
        f"<p class='muted'>{len(fresh)} fresh item(s) since last check "
        f"(moving &ge;3%: {news.get('moving')}).</p>"
        if fresh
        else f"<p class='muted'>No new items since last check "
        f"(moving &ge;3%: {news.get('moving')}).</p>"
    )
    return (
        f"<div class='card'><h3>{t('news_feed')}</h3>"
        f"<p class='muted'>{_esc(source_line)}</p>{items}{fresh_line}</div>"
    )


def _orb_table_html(result: dict, lang: str, limit: int = 50) -> str:
    """ORB hits table card from a scan ``result`` (shared by /api/orb
    and the home page's top-10 — ``limit`` controls how many rows)."""
    t = lambda k: i18n_mod.t(k, lang)
    hits = result.get("hits", [])[:limit]
    failed = result.get("failed", [])

    def _hit_row(h: dict) -> str:
        try:
            stop_mid = (float(h["range_high"]) + float(h["range_low"])) / 2.0
            tp1, tp2, tp3 = _tp123(str(h["direction"]), float(h["close"]), stop_mid)
            tps = f"{tp1:,.2f} / {tp2:,.2f} / {tp3:,.2f}"
        except (KeyError, TypeError, ValueError):
            tps = "—"
        return (
            f"<tr><td>{_esc(h.get('symbol', ''))}</td><td>{_opt(h.get('direction', ''))}</td>"
            f"<td>{_esc(h.get('close', ''))}</td><td>{_esc(h.get('bar_time', ''))}</td>"
            f"<td>{_esc(h.get('range_high', ''))} – {_esc(h.get('range_low', ''))}</td>"
            f"<td>{tps}</td></tr>"
        )

    if not hits:
        return (
            f"<div class='card'><p class='empty-state'>No ORB hits yet today (fetched {result.get('fetched')}, "
            f"usable {result.get('usable')}, as of {_esc(result.get('as_of', ''))}).</p></div>"
        )
    rows = "".join(_hit_row(h) for h in hits)
    failed_html = ""
    if failed:
        failed_html = "<p class='muted'>Not scanned (partial coverage): " + ", ".join(
            _esc(f) for f in failed
        ) + "</p>"
    switcher = " | ".join(
        f"<a href='/orb?symbol={_esc(h.get('symbol', ''))}&lang={lang}'>"
        f"{_esc(h.get('symbol', ''))}</a>"
        for h in hits[:12]
    )
    switch_html = f"<p class='muted'>Chart symbol: {switcher}</p>" if switcher else ""
    more_html = ""
    if limit < len(result.get("hits", [])):
        more_html = f"<p><a href='/orb?lang={lang}'>Full scan — all {len(result.get('hits', []))} hits</a></p>"
    return (
        f"<div class='card'>{switch_html}"
        f"<p>Fetched {result.get('fetched')} symbols, usable {result.get('usable')}, "
        f"hits {len(result.get('hits', []))} (as of {_esc(result.get('as_of', ''))}). "
        f"A hit is the first completed 5m bar that CLOSES above/below the 09:15–09:30 range — "
        f"wicks do not count, and a hit is stale once the price has moved on.</p>"
        f"<div class='table-wrap'><table style='min-width:600px'>"
        f"<colgroup><col style='width:14%'><col style='width:12%'><col style='width:16%'>"
        f"<col style='width:14%'><col style='width:20%'><col style='width:24%'></colgroup>"
        f"<tr><th>Symbol</th><th>Direction</th><th>Close</th><th>Bar</th>"
        f"<th>Range high – low</th><th>TP1 / TP2 / TP3</th></tr>{rows}</table></div>{failed_html}{more_html}</div>"
    )


def _breakdown_for(lang: str, symbol: str, direction: str, entry: float,
                   stop: float, target: float, support=None, resistance=None,
                   trigger_time=None, yahoo_symbol=None) -> str:
    """Signal breakdown card HTML for one setup — news, Time Machine
    and volume vote on the entry, not just the price action. Served
    lazily via /api/orbbreakdown and /api/levelsbreakdown so the
    (potentially slow) news/bars/MC fetches never block a page."""
    try:
        sig = _entry_signal_for(
            symbol, direction, entry, stop, target,
            support=support, resistance=resistance,
            trigger_time=trigger_time, yahoo_symbol=yahoo_symbol,
        )
        return _signal_breakdown_html(sig)
    except Exception as exc:  # noqa: BLE001 - a failed breakdown never blocks the page
        return (
            "<div class='card'><h3>Signal breakdown</h3>"
            f"<p>Unavailable — {_esc(exc)}</p></div>"
        )


def home(lang: str) -> str:
    """Home page — renders INSTANTLY from local data only (paper
    summary). Index quotes, intraday charts, flow, sector map, news,
    ORB hits, robot status, levels and Time Machine summaries all load
    lazily from /api/* after the page — no fetch ever blocks a page
    render. Each section shows a "loading" state until its data
    arrives; a failed fetch shows an honest message, never a blank box.
    The live engine keeps every section warm in the background, so a
    repeat visit shows data at once."""
    t = lambda k: i18n_mod.t(k, lang)
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

    # (a) Index intraday charts — lazy, one card per index. The loader
    # calls are appended to the scripts section (after _chart_js()
    # defines bhanuLazyLine) — never inline in the card body, or the
    # call runs before the function exists (see _line_chart_block).
    chart_cards = ""
    lazy_calls = ""
    for yahoo_symbol, label in (("^NSEI", "NIFTY 50"), ("^NSEBANK", "BANK NIFTY"), ("^BSESN", "SENSEX")):
        cid = "chart-" + label.split()[0].lower().replace(" ", "")
        chart_cards += _line_chart_block(cid, label, yahoo_symbol)
        lazy_calls += f"bhanuLazyLine('{cid}', '{yahoo_symbol}');"

    # (b) Smart Money — all stocks' intraday % change on one spaghetti
    # chart, waterfall tiles on the right (strongest / weakest).
    flow_html = (
        "<div class='card' id='smartmoney'><div class='ml-h'>"
        "<h3>SMART MONEY</h3>"
        "<span class='sub'>Tracked F&O stocks and indices · "
        "intraday price change</span>"
        "<span class='sp'></span>"
        "<a href='#sectormap'>Sector Map ↓</a>"
        "<span class='pill'>5 min scan</span></div>"
        "<div class='sm-stage'><div class='sm-spaghetti'>"
        "<div class='counts' id='flow-chart-counts' style='display:flex;gap:16px'></div>"
        "<div id='flow-chart' style='height:560px'></div></div>"
        "<div class='sm-waterfall' id='flow-chart-waterfall'></div></div>"
        "<p class='note'>Lines: every F&O stock's move since the open "
        "(green above, red below), NIFTY dashed blue. Waterfall: every "
        "stock now, strongest at the top, weakest at the bottom. "
        "Flow refreshes every 5 minutes; upstream data can be delayed. "
        "A technical screen, not advice.</p>"
        "<p class='muted' id='flow-chart-legend'></p></div>"
    )
    # (c) Sector Map horizon (lazy, same data as the flow).
    sector_html = (
        "<div class='card' id='sectormap'><div class='ml-h'>"
        "<h3>SECTOR MAP</h3>"
        "<span class='sub'>Where the money is going — how each sector moves "
        "against NIFTY, with size and momentum</span>"
        "<span class='sp'></span>"
        "<a href='#smartmoney'>Smart money ↑</a>"
        "<span class='pill' id='index-feed-state' data-recent='LIVE · NSE indices polling · recent source ticks'>Waiting for source timestamps</span></div>"
        "<div class='hz-wrap'><div id='sector-map'></div>"
        "<div class='hz-side' id='sector-map-list'></div></div>"
        "<p class='muted'><a href='/sectors'>Full sector page</a>.</p></div>"
    )
    # (d) News feed (prominent section, real-time) — lazy.
    news_html = (
        f"<div id='news-feed'><div class='card'><h3>{t('news_feed')}</h3>"
        f"<p class='loading'>News load ho raha hai…</p></div></div>"
    )
    # (e) ORB hits — lazy (top 10).
    orb_html = (
        "<div class='card' id='orb-hits'><h3>ORB Hits — today</h3>"
        "<p class='loading'>ORB scan load ho raha hai…</p></div>"
    )
    # (f) Robot status — the live engine, in its own card (lazy).
    robot_html = (
        "<div id='robot-status'>"
        "<p class='loading'>Robot status load ho raha hai…</p></div>"
    )
    # (g) Levels mini-summary — support/resistance at a glance (lazy).
    levels_mini_html = (
        "<div class='card' id='levels-mini'><h3>Levels — indices</h3>"
        "<p class='loading'>Levels load ho raha hai…</p></div>"
    )
    # (h) Time Machine mini-summary — today's setup + replay odds (lazy).
    tm_mini_html = (
        "<div class='card' id='tm-mini'><h3>Time Machine — today</h3>"
        "<p class='loading'>Time Machine load ho raha hai…</p></div>"
    )
    body = (
        f"<div class='card'><h3>{t('home')} — indices</h3>"
        f"{_index_cards_html(lang)}"
        f"{paper_html}"
        f"<p>{t('flat_by')}: 15:15 IST. ORB range 09:15–09:30 IST.</p></div>"
        f"<div class='index-charts'>{chart_cards}</div>{flow_html}{sector_html}{robot_html}{news_html}{orb_html}"
        f"{levels_mini_html}{tm_mini_html}"
    )
    return _page(
        lang, t("home"), body,
        head_extra=_chart_lib_tag(),
        scripts=_chart_js()
        + f"<script>bhanuFlowChart('flow-chart');bhanuSectorMap('sector-map');"
          f"bhanuQuoteRefresh();bhanuNewsRefresh('{lang}');bhanuOrbHitsFetch();"
          f"bhanuRobotFetch();bhanuLevelsMiniFetch();bhanuTmMiniFetch();{lazy_calls}</script>",
        live=True,
        active="home",
    )


def _tp123(direction: str, entry: float, stop: float) -> tuple[float, float, float]:
    """TP1 / TP2 / TP3 (1R / 2R / 3R) from an entry and a stop."""
    risk = abs(entry - stop)
    sign = 1.0 if direction == "long" else -1.0
    return (
        round(entry + sign * risk, 2),
        round(entry + sign * 2.0 * risk, 2),
        round(entry + sign * 3.0 * risk, 2),
    )


def _mc_for_symbol(symbol: str, direction: str) -> dict:
    """Monte Carlo result for ``symbol``/``direction`` — but only when
    the cached /timemachine replay is for exactly this setup. A replay
    for another symbol says so plainly instead of being reused, and no
    fresh 10,000-replay run is started from a page load."""
    try:
        data = _cached("timemachine", 600, timecheck_mod.collect_timemachine)
    except Exception as exc:  # noqa: BLE001
        return {"available": False,
                "reason": f"Time Machine unavailable — {exc}."}
    mc = (data or {}).get("monte_carlo", {})
    setup = (data or {}).get("setup") or {}
    if (mc.get("available") and setup.get("symbol") == symbol
            and setup.get("signal") == direction):
        return mc
    return {
        "available": False,
        "reason": (
            "Time Machine replay is for today's best index setup "
            f"({setup.get('symbol', '?')} {setup.get('signal', '')}) — "
            "not this symbol."
        ),
    }


def _entry_signal_for(
    symbol: str,
    direction: str,
    entry: float,
    stop: float,
    target: float,
    support: float | None = None,
    resistance: float | None = None,
    trigger_time: str | None = None,
    yahoo_symbol: str | None = None,
) -> dict:
    """Combined entry signal (ORB + levels + news + Time Machine +
    volume) for one setup. News is cached 5 min, the Time Machine
    result is shared with /timemachine (10 min), and the volume bars
    are cached 60s — so the breakdown costs no extra slow fetch."""
    try:
        news_result = _cached(
            f"news:{symbol}", 300, lambda: news_mod.check_symbol(symbol))
    except Exception:  # noqa: BLE001 - news failure never blocks the page
        news_result = {"unavailable": True}
    try:
        bars = _cached(
            f"signalbars:{yahoo_symbol}", 60,
            lambda: data_mod.fetch_intraday_bars(yahoo_symbol, interval="5m"))
    except Exception:  # noqa: BLE001 - volume degrades to neutral
        bars = None
    volume_result = volume_mod.analyse_volume(
        symbol, bars=bars, trigger_time=trigger_time,
        yahoo_symbol=yahoo_symbol)
    return signal_mod.entry_signal(
        symbol, direction, entry, stop, target,
        support=support, resistance=resistance, bars=bars,
        trigger_time=trigger_time, yahoo_symbol=yahoo_symbol,
        news_result=news_result, mc_result=_mc_for_symbol(symbol, direction),
        volume_result=volume_result,
    )


def _rr_bar_html(entry, stop, tp2) -> str:
    """R:R bar for a trade panel: red risk segment vs green reward
    segment (TP2 = 2R by construction), with SL / R:R / TP2 labels."""
    try:
        risk = abs(float(entry) - float(stop))
        reward = abs(float(tp2) - float(entry))
    except (TypeError, ValueError):
        return ""
    total = risk + reward
    if total <= 0:
        return ""
    rw = 100.0 * risk / total
    rr = reward / risk if risk > 0 else 0.0
    return (
        f"<div class='rr'><i style='width:{rw:.0f}%;background:#d6455d'></i>"
        f"<i style='width:{100.0 - rw:.0f}%;background:#2f9e5f'></i></div>"
        f"<div class='rrl'><span>SL {float(stop):,.2f}</span>"
        f"<span>R:R 1:{rr:.1f}</span><span>TP2 {float(tp2):,.2f}</span></div>"
    )


def _signal_breakdown_html(sig: dict) -> str:
    """Signal breakdown as a trade panel (one entry, one exit): entry /
    SL / TP1/TP2/TP3, R:R bar, and the five factor verdicts that built
    it — news, Time Machine (10,000 replays) and volume included."""
    if not sig or sig.get("decision") == "NO SIGNAL":
        return (
            "<div class='tpanel'><div class='tt'><span>SIGNAL</span>"
            "<span>NO SIGNAL</span></div>"
            "<div class='st none'><b>NO SIGNAL</b> — no ORB trigger, "
            "no entry to judge.</div></div>"
        )

    def _f(value) -> str:
        return f"{value:,.2f}" if value is not None else "—"

    checks = ""
    for key, label in (("orb", "ORB"), ("levels", "Levels"),
                       ("news", "News"), ("mc", "Time Machine"),
                       ("volume", "Volume")):
        factor = (sig.get("factors") or {}).get(key)
        if not factor:
            continue
        checks += (
            f"<div><b>{label} — {_esc(factor.get('verdict', ''))}</b> — "
            f"{_esc(factor.get('reason', ''))}</div>"
        )
    size_note = ""
    if sig.get("size_factor") == 0.5:
        size_note = " (half size)"
    elif sig.get("size_factor") == 0.0:
        size_note = " (no trade)"
    decision = _esc(sig.get("decision", ""))
    st_class = "sig" if decision == "ENTER" else ("warn" if "HALF" in decision else "none")
    rr_txt = "—"
    try:
        risk = abs(float(sig.get("entry")) - float(sig.get("stop")))
        reward = abs(float(sig.get("tp2")) - float(sig.get("entry")))
        if risk > 0:
            rr_txt = f"1:{reward / risk:.1f}"
    except (TypeError, ValueError):
        pass
    robot_line = ""
    try:
        st = poller_mod.status()
        if st.get("running"):
            ticks = st.get("ticks", {})
            robot_line = (
                f"<p class='muted'>Robot — data as of "
                f"{_esc(ticks.get('intraday', '—'))} · news checked "
                f"{_esc(st.get('news_checked_at', '—'))} · "
                f"{st.get('symbols_scanned', 0)} symbols scanned</p>"
            )
    except Exception:  # noqa: BLE001 - the breakdown never depends on the engine
        robot_line = ""
    return (
        f"<div class='tpanel'>"
        f"<div class='tt'><span>SIGNAL — {_esc(sig.get('symbol', ''))} "
        f"{_opt(sig.get('direction', ''))}</span><span>{decision}</span></div>"
        f"<div class='tn'>{decision}{size_note} "
        f"<span class='muted'>{_esc(sig.get('summary', ''))}</span></div>"
        f"<div class='ts'><div>ENTRY<b>{_f(sig.get('entry'))}</b></div>"
        f"<div>STOP<b>{_f(sig.get('stop'))}</b></div>"
        f"<div>R:R<b>{rr_txt}</b></div></div>"
        f"<div class='tr'><span>TP1 — 1R</span><b>{_f(sig.get('tp1'))}</b></div>"
        f"<div class='tr'><span>TP2 — 2R</span><b>{_f(sig.get('tp2'))}</b></div>"
        f"<div class='tr'><span>TP3 — 3R</span><b>{_f(sig.get('tp3'))}</b></div>"
        f"{_rr_bar_html(sig.get('entry'), sig.get('stop'), sig.get('tp2'))}"
        f"<div class='chk'>{checks}</div>"
        f"<div class='st {st_class}'><b>{decision}</b> — "
        f"{_esc(sig.get('summary', ''))}</div>{robot_line}</div>"
    )


def _orb_scan() -> dict:
    """ORB scan result — live engine first (warmed every 2 min), then
    a fresh watchlist file (<300s), then a cached scan. Shared by /orb
    and /api/orbchart so both see the same hits."""
    warmed = poller_mod.get_orb()
    if warmed is not None:
        return warmed
    watchlist = ROOT / "data" / "watchlist.json"
    if watchlist.exists() and time.time() - watchlist.stat().st_mtime < 300:
        try:
            return json.loads(watchlist.read_text())
        except Exception:  # noqa: BLE001 - fall through to a fresh scan
            pass
    if poller_mod.is_running():
        raise RuntimeError("ORB scan warming up")
    result = _cached("orb", 120, scanner_mod.scan_orb)
    scanner_mod.write_watchlist(result)
    return result


def orb_page(lang: str, query: dict | None = None) -> str:
    """ORB scanner page — renders INSTANTLY. The scan table, candle
    chart and signal breakdown all load lazily from /api/* after the
    page (``bhanuOrbLoad``) — no Yahoo fetch ever blocks a page
    render. Each section shows a "loading" state until its data
    arrives; a failed fetch shows an honest message."""
    t = lambda k: i18n_mod.t(k, lang)
    wanted = ((query or {}).get("symbol") or [""])[0].upper()
    sym_line = (
        f"<p class='muted' id='orb-chart-symbol'>Chart symbol: {_esc(wanted)} — "
        f"5m candles, VWAP, order blocks, S/R.</p>"
        if wanted
        else "<p class='muted' id='orb-chart-symbol'>Chart symbol: loading…</p>"
    )
    body = (
        f"{sym_line}"
        f"{_candle_chart_block('orb-chart', wanted)}"
        f"<div id='orb-breakdown'><p class='loading'>Signal breakdown load ho raha hai…</p></div>"
        f"<div class='card' id='orb-table'><p class='loading'>ORB scan load ho raha hai…</p></div>"
    )
    return _page(
        lang, t("orb_scanner"), body,
        head_extra=_chart_lib_tag(),
        scripts=_chart_js() + f"<script>bhanuOrbLoad('{wanted}');</script>",
        live=True,
        active="orb",
    )


def _levels_table_html(result: dict, lang: str) -> str:
    """Levels table card from a levels scan ``result`` (shared by
    /api/levels)."""
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

    def _tps(row: dict) -> tuple[str, str, str]:
        tp1, tp2, tp3 = row.get("tp1"), row.get("tp2"), row.get("tp3")
        if tp1 is None or tp2 is None or tp3 is None:
            try:
                tp1, tp2, tp3 = _tp123(
                    str(row["signal"]), float(row["entry"]), float(row["stop"]))
            except (KeyError, TypeError, ValueError):
                return "—", "—", "—"
        return _fmt(tp1), _fmt(tp2), _fmt(tp3)

    def _body_row(r: dict) -> str:
        tp1, tp2, tp3 = _tps(r)
        return (
            f"<tr><td>{_esc(r.get('symbol', ''))}</td><td>{_fmt(r.get('price'))}</td>"
            f"<td>{_fmt(r.get('nearest_support'))}</td><td>{_fmt(r.get('nearest_resistance'))}</td>"
            f"<td>{_fmt(r.get('pdh'))}</td><td>{_fmt(r.get('pdl'))}</td>"
            f"<td>{_fmt(r.get('entry'))}</td><td>{_sl(r)}</td>"
            f"<td>{tp1}</td><td>{tp2}</td><td>{tp3}</td>"
            f"<td>{_opt(r.get('signal')) if r.get('signal') else 'None'}</td></tr>"
        )

    body_rows = "".join(_body_row(r) for r in rows)
    failed_html = ""
    if failed:
        failed_html = "<p class='muted'>Not computed (partial coverage): " + ", ".join(
            _esc(f"{f['symbol']}: {f['reason']}") for f in failed
        ) + "</p>"
    switcher = " | ".join(
        f"<a href='/levels?symbol={_esc(r.get('symbol', ''))}&lang={lang}'>"
        f"{_esc(r.get('symbol', ''))}</a>"
        for r in rows
    )
    switch_html = f"<p class='muted'>Chart symbol: {switcher}</p>" if switcher else ""
    note = (
        "<p class='muted'>Levels come from Yahoo 5m bars (last 5 sessions) + "
        "daily bars — public, delayed data; verify on the live platform "
        "before any order. Support/resistance = recent swing highs/lows, "
        "previous-day high/low (PDH/PDL) and today's 09:15-09:30 opening range. "
        "ORB entry = first completed 5m bar closing outside the range; "
        "SL = range midpoint (1.5x ATR stop shown alongside); "
        "TP1/TP2/TP3 = 1R/2R/3R; flat by "
        "15:15 IST. A signal is stale once the price has moved on. "
        "The NSE option chain is often blocked (HTTP 403) — no chain data is "
        "faked here; see /chain.</p>"
    )
    return (
        f"<div class='card'>{switch_html}"
        f"<p>As of {_esc(result.get('run_at_ist', ''))}. One row per index — "
        f"enter here, exit here, support here, resistance here.</p>"
        f"<div class='table-wrap'><table style='min-width:720px'>"
        f"<colgroup><col style='width:8%'><col style='width:8%'><col style='width:9%'>"
        f"<col style='width:9%'><col style='width:8%'><col style='width:8%'>"
        f"<col style='width:8%'><col style='width:12%'><col style='width:8%'>"
        f"<col style='width:7%'><col style='width:7%'><col style='width:8%'></colgroup>"
        f"<tr><th>Symbol</th><th>Price</th><th>Nearest Support</th>"
        f"<th>Nearest Resistance</th><th>PDH</th><th>PDL</th><th>ORB Entry</th>"
        f"<th>SL</th><th>TP1</th><th>TP2</th><th>TP3</th><th>Signal</th></tr>{body_rows}</table></div>"
        f"{failed_html}{note}</div>"
    )


def levels_page(lang: str, query: dict | None = None) -> str:
    """Levels page — renders INSTANTLY. The levels table, candle
    chart and signal breakdown all load lazily from /api/* after the
    page (``bhanuLevelsLoad``) — no Yahoo fetch ever blocks a page
    render. Each section shows a "loading" state until its data
    arrives; a failed fetch shows an honest message."""
    t = lambda k: i18n_mod.t(k, lang)
    wanted = ((query or {}).get("symbol") or ["NIFTY"])[0].upper()
    sym_line = (
        f"<p class='muted' id='levels-chart-symbol'>Chart symbol: {_esc(wanted)} — "
        f"5m candles, VWAP, order blocks, S/R.</p>"
    )
    body = (
        f"{sym_line}"
        f"{_candle_chart_block('levels-chart', wanted)}"
        f"<div id='levels-breakdown'><p class='loading'>Signal breakdown load ho raha hai…</p></div>"
        f"<div class='card' id='levels-table'><p class='loading'>Levels load ho raha hai…</p></div>"
    )
    return _page(
        lang, t("levels"), body,
        head_extra=_chart_lib_tag(),
        scripts=_chart_js() + f"<script>bhanuLevelsLoad('{wanted}');</script>",
        live=True,
        active="levels",
    )


def _stock_setup_html(sig: dict | None, row: dict | None, stats: dict | None) -> str:
    """Right-side SETUP card for the /stock detail page (SETUP READY
    card, our own data): trigger, entry/SL/TP1/TP2/TP3,
    risk in ₹ (stocks) or points (indices) and %, R:R, the
    combined-signal summary, and the symbol's real ORB backtest
    stats. No signal -> WATCHING with the pending trigger (buy above
    the ORB high / sell below the low on a 5m close) and nearest
    support/resistance."""

    def _f(value) -> str:
        return f"{value:,.2f}" if value is not None else "—"

    row = row or {}
    if stats and stats.get("available"):
        tot_fires = stats["long"]["fires"] + stats["short"]["fires"]
        tot_wins = stats["long"]["wins"] + stats["short"]["wins"]
        tot_losses = stats["long"]["losses"] + stats["short"]["losses"]
        net_r = stats["long"]["net_r"] + stats["short"]["net_r"]
        avg_r = (
            (stats["long"]["avg_r"] * stats["long"]["fires"]
             + stats["short"]["avg_r"] * stats["short"]["fires"]) / tot_fires
            if tot_fires else 0.0
        )
        dec = tot_wins + tot_losses
        wr = f"{100.0 * tot_wins / dec:.1f}%" if dec else "—"
        stats_html = (
            f"<div class='setup-stats'>"
            f"<div>Trades<b>{tot_fires}</b></div>"
            f"<div>Win rate<b>{wr}</b></div>"
            f"<div>Net R<b>{net_r:+.2f}R</b></div>"
            f"<div>Avg / trade<b>{avg_r:+.3f}R</b></div></div>"
            f"<p class='muted'>ORB backtest — {stats['sessions']} sessions "
            f"({stats['first_day']} to {stats['last_day']}), 5m bars. "
            f"Long {stats['long']['fires']} fires · "
            f"Short {stats['short']['fires']} fires.</p>"
        )
    else:
        reason = (stats or {}).get("reason", "no data")
        stats_html = f"<p class='muted'>Backtest unavailable — {_esc(reason)}.</p>"
    if not sig or sig.get("decision") == "NO SIGNAL":
        orb_high, orb_low = row.get("orb_high"), row.get("orb_low")
        trigger = ""
        if orb_high is not None and orb_low is not None:
            trigger = (f"Buy above {_f(orb_high)} / sell below {_f(orb_low)} "
                       f"on a 5m close.")
        return (
            f"<div class='card'><h3>SETUP</h3><div class='tpanel'>"
            f"<div class='tt'><span>SETUP</span><span>WATCHING</span></div>"
            f"<div class='st none'><b>WATCHING</b> — no ORB trigger yet today. {trigger} "
            f"Nearest support {_f(row.get('nearest_support'))}, resistance "
            f"{_f(row.get('nearest_resistance'))}.</div>{stats_html}</div></div>"
        )
    direction = sig["direction"]
    symbol = str(sig.get("symbol", ""))
    unit = "pts" if symbol in data_mod.INDICES else "₹"
    entry, stop = float(sig["entry"]), float(sig["stop"])
    tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]
    risk = abs(entry - stop)
    risk_pct = 100.0 * risk / entry if entry else 0.0
    decision = sig["decision"]
    st_class = "sig" if decision == "ENTER" else ("warn" if "HALF" in decision else "none")
    title = {"ENTER": "SETUP READY", "HALF SIZE": "HALF SIZE",
             "BLOCKED": "BLOCKED"}.get(decision, decision)
    return (
        f"<div class='card'><h3>SETUP</h3><div class='tpanel'>"
        f"<div class='tt'><span>SETUP — {_opt(direction)}</span><span>{title}</span></div>"
        f"<div class='tn'>{title} — triggered @ {_f(entry)} "
        f"<span class='muted'>{_esc(sig.get('summary', ''))}</span></div>"
        f"<div class='ts'><div>ENTRY<b>{_f(entry)}</b></div>"
        f"<div>STOP<b>{_f(stop)}</b></div>"
        f"<div>RISK<b>{_f(risk)} {unit} ({risk_pct:.2f}%)</b></div></div>"
        f"<div class='tr'><span>TP1 — 1R</span><b>{_f(tp1)}</b></div>"
        f"<div class='tr'><span>TP2 — 2R</span><b>{_f(tp2)}</b></div>"
        f"<div class='tr'><span>TP3 — 3R</span><b>{_f(tp3)}</b></div>"
        f"{_rr_bar_html(entry, stop, tp2)}"
        f"<div class='st {st_class}'><b>{decision}</b> — {_esc(sig.get('summary', ''))}</div>"
        f"{stats_html}</div></div>"
    )


def stock_page(lang: str, query: dict | None = None) -> str:
    """Per-stock detail page — renders INSTANTLY. Header, setup card,
    summary and chart all load lazily from /api/stock
    (``bhanuStockLoad``). Timeframe buttons switch the chart
    interval; overlay pills toggle the chart overlays (an unavailable
    overlay's pill is disabled)."""
    symbol = ((query or {}).get("symbol") or ["NIFTY"])[0].upper()
    symbol = "".join(c for c in symbol if c.isalnum() or c in ".^") or "NIFTY"
    tf_buttons = "".join(
        f"<button class='tfbtn{' on' if iv == '5m' else ''}' data-iv='{iv}' "
        f"onclick=\"bhanuStockLoad('{symbol}','{iv}')\">{iv}</button>"
        for iv in ("1m", "5m", "15m", "1H", "D")
    )
    pills = "".join(
        f"<button class='opill on' data-ov='{key}' "
        f"onclick=\"bhanuToggleOverlay('stock-chart','{key}',this)\">{label}</button>"
        for key, label in (
            ("orderblock", "Order block"), ("fvg", "Fair value gaps"),
            ("structure", "Structure"), ("liquidity", "Liquidity"),
            ("vwap", "VWAP"), ("ema", "EMA 20"), ("volume", "Volume"), ("trades", "Trades"),
        )
    )
    note = (
        "Candles: interval buttons se badlo. Purple line = VWAP. Dashed lines = "
        "ORB range / PDH / PDL / support / resistance. Green box = target zone, "
        "red box = stop zone, pale boxes = order blocks / fair value gaps. "
        "Arrows = CHoCH / BOS breaks. Daily chart par purane ORB trades ke "
        "win/loss markers bhi dikhte hain."
    )
    body = (
        f"<div id='stock-body'>"
        f"<div class='card' id='stock-head'><p class='loading'>Stock load ho raha hai…</p></div>"
        f"<div class='card'><div id='stock-tf'>{tf_buttons}</div>"
        f"<div id='stock-pills' style='margin-top:8px'>{pills}</div></div>"
        f"<div class='stock-grid'>"
        f"<div>{_candle_chart_block('stock-chart', symbol, note=note)}</div>"
        f"<div id='stock-setup'><p class='loading'>Setup load ho raha hai…</p></div>"
        f"</div>"
        f"<div id='stock-summary'><p class='loading'>Summary load ho raha hai…</p></div>"
        f"</div>"
    )
    return _page(
        lang, f"{symbol} — Stock detail", body,
        head_extra=_chart_lib_tag(),
        scripts=_chart_js() + f"<script>bhanuStockLoad('{symbol}','5m');</script>",
        live=True,
        active="",
    )


def _api_stock(query: dict) -> dict:
    """Full /stock detail payload: header, setup card, chart, summary.

    Cached 120 s per symbol+interval. Every number is real (Yahoo/NSE
    free data) or the section says "unavailable" — nothing invented.
    """
    symbol_q = (query.get("symbol") or ["NIFTY"])[0]
    interval = (query.get("interval") or ["5m"])[0]
    try:
        label, yahoo_symbol, in_universe = stock_mod.resolve_symbol(symbol_q)
    except ValueError as exc:
        return {"symbol": symbol_q, "error": str(exc)}

    def _build() -> dict:
        try:
            q = data_mod.fetch_quote(yahoo_symbol)
            price, prev_close, change_pct = (
                q.get("price"), q.get("prev_close"), q.get("change_pct"))
        except Exception:  # noqa: BLE001 - price just unavailable
            price = prev_close = change_pct = None
        flow = poller_mod.get_flow()
        if flow is None and not poller_mod.is_running():
            try:
                flow = _cached("api:flow", 300, charts_mod.flow_series)
            except Exception:  # noqa: BLE001
                flow = None
        rank = stock_mod.rank_in_flow(label, flow) if in_universe else None
        sector = stock_mod.sector_for(label)
        try:
            row = _cached(
                f"levelsrow:{label}", 60,
                lambda: levels_mod.compute_levels(label, yahoo_symbol),
            )
        except Exception:  # noqa: BLE001
            row = None
        sig = None
        if row and row.get("signal") and row.get("entry") and row.get("stop") and row.get("target"):
            try:
                sig = _entry_signal_for(
                    label, row["signal"], row["entry"], row["stop"], row["target"],
                    support=row.get("nearest_support"),
                    resistance=row.get("nearest_resistance"),
                    yahoo_symbol=yahoo_symbol,
                )
            except Exception:  # noqa: BLE001 - setup card degrades to WATCHING
                sig = None
        try:
            hist = stock_mod.orb_history(yahoo_symbol)
        except Exception:  # noqa: BLE001
            hist = {"available": False, "reason": "backtest failed"}
        past_trades = hist.get("trades", []) if hist.get("available") else []
        try:
            chart = _cached(
                f"stockchart:{label}:{interval}", 120,
                lambda: charts_mod.stock_chart(
                    yahoo_symbol, label, interval, row=row, past_trades=past_trades),
            )
        except Exception as exc:  # noqa: BLE001
            chart = {"symbol": label, "candles": [], "error": str(exc)}
        summary_text, summary_source = stock_mod.simple_words(label, yahoo_symbol)
        if rank:
            rank_txt = (
                f"{'BULLISH' if rank['up'] else 'BEARISH'} "
                f"#{rank['rank']} OF {rank['total']}"
            )
            rank_html = (
                f"<span class='tag {'bull' if rank['up'] else 'bear'}'>{rank_txt}</span>"
            )
        else:
            rank_html = "<span class='muted'>Rank unavailable</span>"
        if price is not None and change_pct is not None:
            price_html = (
                f"<span class='px'>{price:,.2f}</span>"
                f"<span class='{'up' if change_pct >= 0 else 'down'}'>"
                f"{'+' if change_pct >= 0 else ''}{change_pct:.2f}%</span>"
                + (f"<span class='muted'>prev close {prev_close:,.2f}</span>"
                   if prev_close is not None else "")
            )
        else:
            price_html = "<span class='muted'>Price unavailable</span>"
        header_html = (
            f"<div class='stock-head'><h2 style='margin:0'>{_esc(label)}</h2>"
            f"<span class='muted'>{_esc(sector) if sector else 'Sector unavailable'}</span>"
            f"{rank_html}{price_html}</div>"
        )
        if summary_text:
            summary_html = (
                f"<div class='card'><h3>IN SIMPLE WORDS</h3><p>{_esc(summary_text)}</p>"
                f"<p class='muted'>Source: {_esc(summary_source)}.</p></div>"
            )
        else:
            summary_html = (
                "<div class='card'><h3>IN SIMPLE WORDS</h3>"
                "<p class='muted'>Company summary unavailable — Yahoo se nahi mila.</p></div>"
            )
        return {
            "symbol": label,
            "yahoo_symbol": yahoo_symbol,
            "in_universe": in_universe,
            "sector": sector,
            "price": price,
            "prev_close": prev_close,
            "change_pct": change_pct,
            "rank": rank,
            "header_html": header_html,
            "setup_html": _stock_setup_html(sig, row, hist),
            "summary_html": summary_html,
            "chart": chart,
            "stats": hist,
            "error": None,
        }

    try:
        return _cached(f"stock:{label}:{interval}", 120, _build)
    except Exception as exc:  # noqa: BLE001
        return {"symbol": label, "error": str(exc)}


def sectors_page(lang: str) -> str:
    t = lambda k: i18n_mod.t(k, lang)
    body = (
        "<div class='card'><h3>Sector Map — bubble chart</h3>"
        "<p class='muted'>Each bubble is a sector: green = up today, red = down, "
        "size = today's traded value (close × volume). Position: x = sector vs "
        "NIFTY (right = beating NIFTY), y = sector % change. The map loads "
        "after the page (it fetches all 39 symbols) — if it fails, it says so.</p>"
        "<div id='sector-map'></div><div id='sector-map-list'></div></div>"
        "<div class='card'><h3>How it is built</h3>"
        "<p class='muted'>Sectors are the NSE's usual groupings of the 36 stocks "
        "in our universe; each sector's % change is the equal-weighted mean of "
        "its members' intraday % change from the open. Market caps are not in "
        "our data, so bubble size uses today's traded value instead — a real "
        "weight, labelled as such. A sector whose members all failed to "
        "fetch is listed as failed, never shown as flat.</p></div>"
    )
    return _page(
        lang, "Sectors", body,
        scripts=_chart_js() + "<script>bhanuSectorMap('sector-map');</script>",
        live=True,
        active="sectors",
    )


def skills_page(lang: str) -> str:
    """Skills page — renders INSTANTLY. The live skill statuses
    (which take ~12 s cold, they fetch live data) load lazily from
    /api/skills after the page, like every other slow section."""
    t = lambda k: i18n_mod.t(k, lang)
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
        f"<div class='card'><div id='skills-table'>"
        f"<p class='loading'>Skills load ho raha hai…</p></div>{note}</div>"
        f"<div id='news-feed'><p class='loading'>{t('news_feed')} · loading…</p></div>"
    )
    return _page(lang, t("skills"), body,
                 scripts=_chart_js() + f"<script>bhanuSkillsLoad();bhanuNewsRefresh('{lang}');</script>",
                 active="skills")


def _api_skills() -> dict:
    """Live skill statuses (cached 120 s) — the slow part of /skills,
    served lazily so the page itself opens instantly."""
    try:
        rows = _cached("skills", 120, lambda: skills_mod.collect_skill_status(live=True))
    except Exception as exc:  # noqa: BLE001
        return {"html": f"<p>skills unavailable: {_esc(exc)}</p>"}
    body_rows = "".join(
        f"<tr><td>{_esc(r.get('daytrader', ''))}</td><td>{_esc(r.get('module', ''))}</td>"
        f"<td>{_esc(r.get('does', ''))}</td><td>{_esc(r.get('shows', ''))}</td>"
        f"<td>{_esc(r.get('status', ''))}</td></tr>"
        for r in rows
    )
    return {"html": (
        f"<p>{len(rows)} skills — what each does, where it shows, and its live status right now.</p>"
        f"<div class='table-wrap'><table>"
        f"<colgroup><col style='width:16%'><col style='width:14%'><col style='width:34%'>"
        f"<col style='width:16%'><col style='width:20%'></colgroup>"
        f"<tr><th>DayTrader skill</th><th>Our module</th><th>What it does</th>"
        f"<th>Shows on</th><th>Live status</th></tr>{body_rows}</table></div>"
    )}


def _timemachine_body(data: dict) -> str:
    """Full Time Machine body HTML from a ``collect_timemachine``
    result — setup, Monte Carlo, trade check, backtest. Shared by
    /api/timemachine (lazy page load)."""
    def _fmt(value) -> str:
        return f"{value:,.2f}" if value is not None else "—"

    setup = data.get("setup")
    setup_html = ""
    if setup:
        setup_html = (
            f"<div class='card'><h3>Today's best ORB setup</h3>"
            f"<p>{_esc(setup.get('symbol', ''))} {_opt(setup.get('signal', ''))} — "
            f"entry {_fmt(setup.get('entry'))}, stop {_fmt(setup.get('stop'))}"
            + (f" (1.5x ATR {_fmt(setup.get('atr_stop'))})" if setup.get("atr_stop") else "")
            + f", target {_fmt(setup.get('target'))}, "
            f"range {_fmt(setup.get('orb_low'))}–{_fmt(setup.get('orb_high'))}, "
            f"price now {_fmt(setup.get('price'))}.</p></div>"
        )
    else:
        setup_html = f"<div class='card'><h3>Today's best ORB setup</h3><p>{_esc(data.get('setup_error', 'no setup'))}</p></div>"

    mc = data.get("monte_carlo", {})
    if mc.get("available"):
        hist_html = ""
        if mc.get("histogram"):
            hist_html = (
                f"<div class='card'><div id='mc-hist'>"
                f"{_mc_histogram_svg(mc['histogram'])}</div></div>"
            )
        mc_html = (
            hist_html
            + f"<div class='card' id='mc-card'><h3 id='mc-title'>Monte Carlo Time Machine — {mc['n']:,} replays</h3>"
            f"<p id='mc-setup'>{_esc(mc.get('symbol', ''))} {_opt(mc.get('direction', ''))} "
            f"entry {_fmt(mc.get('entry'))}, stop {_fmt(mc.get('stop'))}, "
            f"target {_fmt(mc.get('target'))}.</p>"
            f"<div class='table-wrap'><table><tr><th>Outcome</th><th>Share</th></tr>"
            f"<tr><td>Target first (win)</td><td id='mc-target-pct'>{mc['target_first_pct']:.1f}%</td></tr>"
            f"<tr><td>Stop first (loss)</td><td id='mc-stop-pct'>{mc['stop_first_pct']:.1f}%</td></tr>"
            f"<tr><td>Neither (flat by 15:15)</td><td id='mc-neither-pct'>{mc['neither_pct']:.1f}%</td></tr></table></div>"
            f"<p id='mc-summary'>Average outcome: {mc['avg_r']:+.2f}R. Best case (target hit): "
            f"+{mc['best_case_r']:.1f}R. Worst case (stop hit): {mc['worst_case_r']:.1f}R. "
            f"Verdict: {mc['verdict']} (break-even {mc['break_even_pct']:.1f}% "
            f"target-first among decisive replays). "
            f"Reality check only — resampled bars cannot see the future, and "
            f"fees/slippage are ignored.</p></div>"
        )
    else:
        mc_html = (
            f"<div class='card'><h3>Monte Carlo Time Machine</h3>"
            f"<p>Unavailable — {_esc(mc.get('reason', 'no data'))}</p></div>"
        )

    tc = data.get("trade_check", {})
    if tc.get("available"):
        lim = tc["limits"]
        tc_html = (
            f"<div class='card'><h3>Trade check — {tc['symbol']} {_opt(tc['direction'])}</h3>"
            f"<p>Risk gate: <b>{tc['gate_verdict']}</b>"
            + (f" — {_esc(tc['gate_reason'])}" if tc.get("gate_reason") and tc["gate_reason"] != "ok" else "")
            + f". Sizing: {tc['sizing_verdict']} — {_esc(tc['sizing_reason'])}.</p>"
            f"<div class='table-wrap'><table><tr><th>Four limits (max units)</th><th>Units</th></tr>"
            f"<tr><td>Max loss/trade (₹500 / risk per unit)</td><td>{lim['max_loss']}</td></tr>"
            f"<tr><td>Position cap (75% of budget / entry)</td><td>{lim['position_cap']}</td></tr>"
            f"<tr><td>Max order (₹80,000 / entry)</td><td>{lim['max_order']}</td></tr>"
            f"<tr><td>Cash buffer (90% of budget / entry)</td><td>{lim['cash_buffer']}</td></tr>"
            f"<tr><td><b>Binding limit → qty</b></td><td><b>{tc['qty']}</b> ({_esc(tc.get('binding_limit', ''))})</td></tr></table></div>"
            f"<p>Entry {_fmt(tc.get('entry'))}, stop {_fmt(tc.get('stop'))}, "
            f"target {_fmt(tc.get('target'))}, R:R {tc['rr']:.2f}. "
            f"Position value ₹{tc['position_value']:,.2f}, max loss at stop "
            f"₹{tc['max_loss']:,.2f}. Positions: capital allows "
            f"{tc['positions_by_capital']}, day limit allows "
            f"{tc['positions_by_day']}/day — max {tc['positions_max']} such "
            f"position(s) (budget ₹{tc['budget']:,.0f}).</p></div>"
        )
    else:
        tc_html = (
            f"<div class='card'><h3>Trade check</h3>"
            f"<p>Unavailable — {_esc(tc.get('reason', 'no setup'))}</p></div>"
        )

    bt = data.get("backtest", {})
    bt_rows = ""
    if bt.get("available"):
        for direction in ("long", "short"):
            d = bt["directions"].get(direction, {})
            if d.get("available"):
                bt_rows += (
                    f"<tr><td>ORB {_opt(direction)}</td><td>{d['fires']}</td>"
                    f"<td>{d['target_wins']}</td><td>{d['stop_losses']}</td>"
                    f"<td>{d['neither']}</td><td>{d['avg_r']:+.3f}R</td></tr>"
                )
            else:
                bt_rows += (
                    f"<tr><td>ORB {_opt(direction)}</td><td colspan='5'>unavailable — "
                    f"{_esc(d.get('reason', ''))}</td></tr>"
                )
        bt_html = (
            f"<div class='card'><h3>ORB backtest — NIFTY 5m ({_esc(bt.get('first_day', '?'))} to "
            f"{_esc(bt.get('last_day', '?'))}, {_esc(bt.get('sessions', '?'))} full sessions)</h3>"
            f"<div class='table-wrap'><table><tr><th>Direction</th><th>Fires</th><th>Target wins</th>"
            f"<th>Stop losses</th><th>Neither</th><th>Avg R</th></tr>{bt_rows}</table></div>"
            f"<p class='muted'>History replay on real bars (range-midpoint stop, "
            f"2R target, flat 15:15; no fees/slippage). Small sample, and the "
            f"period was a strong uptrend — do not read this as a general edge. "
            f"No accuracy percentage is claimed.</p></div>"
        )
    else:
        bt_html = (
            f"<div class='card'><h3>ORB backtest</h3>"
            f"<p>Unavailable — {_esc(bt.get('reason', 'no data'))}</p></div>"
        )

    return setup_html + mc_html + tc_html + bt_html


def _tm_check_symbols() -> list[str]:
    """Universe order for the Time Machine form dropdown: indices
    first, then the F&O stocks alphabetically."""
    return list(data_mod.INDICES) + sorted(data_mod.STOCKS)


def _tm_check_result_html(result: dict) -> str:
    """Result card for a user's own Time Machine check (the form's
    answer) — Monte Carlo outcome + trade check, same bands and
    wording as the auto setup's card."""
    def _fmt(value) -> str:
        return f"{value:,.2f}" if value is not None else "—"

    mc = result.get("monte_carlo", {})
    tc = result.get("trade_check", {})
    head = (
        f"<div class='card'><h3>Apna trade — {_esc(result.get('symbol', ''))} "
        f"{_opt(result.get('direction', ''))}</h3>"
        f"<p>Entry {_fmt(result.get('entry'))}, stop {_fmt(result.get('stop'))}, "
        f"target {_fmt(result.get('target'))}.</p>"
    )
    if not mc.get("available"):
        return head + f"<p>Monte Carlo unavailable — {_esc(mc.get('reason', 'no data'))}</p></div>"
    hist_html = ""
    if mc.get("histogram"):
        hist_html = (
            f"<div id='tm-check-hist'>{_mc_histogram_svg(mc['histogram'])}</div>"
        )
    body = (
        hist_html
        + f"<p>Monte Carlo — {mc['n']:,} replays, apne trade ke levels par.</p>"
        f"<div class='table-wrap'><table><tr><th>Outcome</th><th>Share</th></tr>"
        f"<tr><td>Target first (win)</td><td>{mc['target_first_pct']:.1f}%</td></tr>"
        f"<tr><td>Stop first (loss)</td><td>{mc['stop_first_pct']:.1f}%</td></tr>"
        f"<tr><td>Neither (flat by 15:15)</td><td>{mc['neither_pct']:.1f}%</td></tr></table></div>"
        f"<p>Average outcome: {mc['avg_r']:+.2f}R. Best case (target hit): "
        f"+{mc['best_case_r']:.1f}R. Worst case (stop hit): {mc['worst_case_r']:.1f}R. "
        f"Verdict: {mc['verdict']} (break-even {mc['break_even_pct']:.1f}% "
        f"target-first among decisive replays). "
        f"Reality check only — resampled bars cannot see the future, and "
        f"fees/slippage are ignored.</p>"
    )
    if tc.get("available"):
        body += (
            f"<p>Trade check — Risk gate: <b>{tc['gate_verdict']}</b>"
            + (f" — {_esc(tc['gate_reason'])}" if tc.get("gate_reason") and tc["gate_reason"] != "ok" else "")
            + f". Sizing: {tc['sizing_verdict']} — {_esc(tc['sizing_reason'])}.</p>"
            f"<p>Qty {tc['qty']} (binding limit: {_esc(tc.get('binding_limit', ''))}), "
            f"R:R {tc['rr']:.2f}, risk per unit {_fmt(tc.get('risk_per_unit'))}, "
            f"position value ₹{tc['position_value']:,.2f}, max loss at stop "
            f"₹{tc['max_loss']:,.2f}. Max {tc['positions_max']} aisi position(s) "
            f"(budget ₹{tc['budget']:,.0f}).</p>"
        )
    else:
        body += f"<p>Trade check unavailable — {_esc(tc.get('reason', 'no setup'))}</p>"
    return head + body + "</div>"


def timemachine_page(lang: str) -> str:
    """Time Machine page — renders INSTANTLY; the setup / Monte Carlo
    / trade check / backtest body loads lazily from /api/timemachine
    (the 10,000-replay run is slow on a cold cache). The user's own
    trade-check form sits above the lazy body."""
    t = lambda k: i18n_mod.t(k, lang)
    options = "".join(
        f"<option value='{_esc(sym)}'{' selected' if sym == 'NIFTY' else ''}>{_esc(sym)}</option>"
        for sym in _tm_check_symbols()
    )
    form = (
        "<div class='card'><h3>Apna trade check karo — Time Machine</h3>"
        "<p class='muted'>Apna entry, stop aur target daalo — wahi 10,000 replays "
        "tumhare trade par chalenge (symbol ke apne last 60 din ke 5m bars par), "
        "aur trade check bhi milega. Robot, signals ya scans par iska koi asar nahi.</p>"
        "<form id='tm-check-form' method='get' action='/api/timemachine-check' "
        "onsubmit='return bhanuTmCheckSubmit(event);'>"
        f"Symbol <select name='symbol'>{options}</select> "
        "Entry <input name='entry' inputmode='decimal' size='10' required> "
        "Stop <input name='stop' inputmode='decimal' size='10' required> "
        "Target <input name='target' inputmode='decimal' size='10' required> "
        "<select name='direction' aria-label='Direction'>"
        "<option value='long'>CALL</option><option value='short'>PUT</option></select> "
        "<button type='submit'>Check karo — 10,000 replays</button></form>"
        "<p class='muted'>Option ke liye premium series chahiye — ye form index/stock "
        "levels ke liye hai. Option ka premium theta decay se girta hai, isliye spot "
        "bars par uska replay galat hoga.</p>"
        "<div id='tm-check-result'></div></div>"
    )
    body = (
        form
        + """<section class="card"><h3>Strategy Backtesting Lab</h3><p>Ek underlying ka real 5-minute CSV chunein. Use bar-open ISO timestamps (naive = IST) and columns <code>time,open,high,low,close</code>. Maximum 8 MiB.</p>
<form id="history-form"><label>5m CSV <input type="file" accept=".csv,text/csv" required></label>
<label>Strategy <select name="strategy"><option value="orb15">15-minute ORB</option><option value="orb30">30-minute ORB</option><option value="channel">Rolling six-bar breakout</option></select></label>
<label>From <input type="date" name="start" required></label><label>Through <input type="date" name="end" required></label>
<label>Units <input type="number" name="qty" value="1" min="1" max="1000000" required></label>
<label>Brokerage ₹ / order <input type="number" name="fee" min="0" step="any" value="0" required></label>
<label>Slippage bps / side <input type="number" name="slip" min="0" max="9999" step="any" value="0" required></label>
<button>Run replay</button></form><p class="note">First breakout close per complete session, midpoint stop, 2R target. Subsequent bars only. Stop first on ambiguous bars. Exit by 15:15 IST. P&amp;L uses underlying units. Taxes excluded.</p><div id="history-result" role="status"></div></section>
"""
        + "<div id='timemachine-body'>"
        "<p class='loading'>Time Machine load ho raha hai…</p></div>"
    )
    return _page(
        lang, t("timemachine"), body,
        scripts=_chart_js() + "<script>bhanuTimemachineLoad();</script>" + """<script>
document.getElementById('history-form').addEventListener('submit',async function(e){
  e.preventDefault();
  const out=document.getElementById('history-result'), file=this.querySelector('[type=file]').files[0];
  const button=this.querySelector('button');
  out.textContent='Replay chal raha hai…'; button.disabled=true;
  try{
    if(file.size>8388608)throw Error('Maximum CSV size is 8 MiB.');
    const q=new URLSearchParams(new FormData(this));
    const res=await fetch('/api/history?'+q,{method:'POST',body:file});
    const data=await res.json();
    if(data.error)throw Error(data.error);
    const money=n=>n.toLocaleString('en-IN',{maximumFractionDigits:2});
    const total=k=>data.trades.reduce((sum,t)=>sum+t[k],0);
    out.textContent=data.strategy+' · '+data.from+' → '+data.to+' · '+data.complete+' complete sessions · '+data.skipped+' incomplete skipped · '+data.trades.length+' trades · Gross ₹'+money(total('gross'))+' · Costs ₹'+money(total('cost'))+' · Net ₹'+money(total('net'))+' · Max closed-trade drawdown ₹'+money(data.max_drawdown);
    if(data.trades.length){
      const wrap=document.createElement('div'), table=document.createElement('table');
      wrap.className='table-wrap';wrap.append(table);out.append(wrap);
      const head=table.createTHead().insertRow();
      for(const name of ['Date','Side','Exit','Gross ₹','Costs ₹','Net ₹','Drawdown ₹']){const th=document.createElement('th');th.textContent=name;head.append(th);}
      const body=table.createTBody();
      for(const t of data.trades){const row=body.insertRow();for(const v of [t.date,t.side,t.exit,money(t.gross),money(t.cost),money(t.net),money(t.drawdown)])row.insertCell().textContent=v;}
    }
  }catch(err){out.textContent='Replay unavailable · '+err.message;}finally{button.disabled=false;}
});
</script>
""", live=True,
        active="timemachine",
    )


def chain_page(lang: str, query: dict) -> str:
    """Render only the latest background snapshot, including its last failure."""
    symbol = (query.get("symbol") or ["NIFTY"])[0].upper()
    if symbol not in ("NIFTY", "BANKNIFTY"):
        symbol = "NIFTY"
    snapshot = poller_mod.get_chain(symbol)
    chain = snapshot.get("data")
    title = i18n_mod.t("option_chain", lang)
    tabs = "".join(
        f"<a href='?symbol={s}&lang={lang}' aria-current='{str(s == symbol).lower()}'>{s}</a>"
        for s in ("NIFTY", "BANKNIFTY")
    )
    body = f"<div class='chain-controls'>{tabs}</div>"
    error = snapshot.get("error")
    if error:
        body += f"<p class='feed-message' role='status'>Chain update unavailable · {_esc(error)}</p>"
    if not chain:
        if not error:
            message = "NSE chain ka intezaar hai…" if lang == "hi" else "Waiting for the first NSE chain…"
            body += f"<p class='loading' role='status'>{message}</p>"
        body += "<p class='muted'>Background refresh enabled · <a href=''>Check again</a></p>"
        return _page(lang, title, body, head_extra='<meta http-equiv="refresh" content="30">', active="chain")

    expiry = (query.get("expiry") or [chain["expiry"]])[0]
    if expiry not in chain["expiries"]:
        body += "<p class='feed-message'>Selected expiry unavailable. Showing nearest available expiry.</p>"
        expiry = chain["expiry"]
    selected = chain["expiries"][expiry]
    choices = "".join(
        f"<option{' selected' if day == expiry else ''}>{_esc(day)}</option>"
        for day in chain["expiries"]
    )
    body += (
        f"<form class='expiry' method='get'><input type='hidden' name='symbol' value='{symbol}'>"
        f"<input type='hidden' name='lang' value='{lang}'><label>Expiry <select name='expiry'>{choices}"
        "</select></label> <button>Apply</button></form>"
    )

    def fmt(value, spec):
        return format(value, spec) if value is not None else "—"

    metrics = (("Underlying", chain["underlying"], ",.2f"), ("ATM", selected["atm_strike"], ",g"),
               ("PCR", selected["pcr"], ".2f"), ("Max pain", selected["max_pain"], ",g"),
               ("CALL OI", selected["total_ce_oi"], ","), ("PUT OI", selected["total_pe_oi"], ","))
    body += "<div class='metrics'>" + "".join(
        f"<div><small>{label}</small><strong>{fmt(value, spec)}</strong></div>"
        for label, value, spec in metrics
    ) + "</div>"
    strikes = selected["strikes"]
    atm = selected["atm_strike"]
    middle = next((i for i, row in enumerate(strikes) if row["strike"] == atm), 0)
    start = max(0, min(middle - 10, len(strikes) - 21))
    rows = []
    years = greeks.years_left(chain.get("as_on"), expiry)
    peak = {side: max(((r[side] or {}).get("oi") or 0 for r in strikes), default=0)
            for side in ("ce", "pe")}
    for row in strikes[start:start + 21]:
        def cells(side):
            quote = dict(row[side] or {})
            quote.update(greeks.solve(chain["underlying"], row["strike"], quote.get("last_price"), years, side == "ce"))
            spot = chain["underlying"]
            itm = spot is not None and (row["strike"] < spot if side == "ce" else row["strike"] > spot)
            def cell(key, spec):
                maximum = key == "oi" and peak[side] and quote.get("oi") == peak[side]
                css = "peak-oi" if maximum else "itm" if itm else ""
                hint = " · Max OI" if maximum else " · ITM" if itm and key == "last_price" else ""
                return f"<td class='{css}'>{fmt(quote.get(key), spec)}{hint}</td>"
            return "".join(cell(k, spec)
                           for k, spec in (("last_price", ",.2f"), ("iv", ".2f"), ("oi", ","),
                                           ("model_iv", ".2f"), ("delta", ".3f"), ("gamma", ".6f"),
                                           ("theta", ".3f"), ("vega", ".3f")))
        at_money = row["strike"] == atm
        label = " · ATM" if at_money else ""
        if row["strike"] == selected["max_pain"]:
            label += " · MP"
        rows.append(f"<tr{' class=atm' if at_money else ''}>{cells('ce')}"
                    f"<th scope='row'>{row['strike']:,g}{label}</th>{cells('pe')}</tr>")
    body += (
        f"<section class='card'><h3>{symbol} · {_esc(expiry)}</h3>"
        f"<p class='muted'>{'Last good snapshot' if error else 'Snapshot'} · "
        f"{_esc(chain.get('source', 'NSE'))} as of {_esc(chain.get('as_on') or 'unknown')} · received {_esc(snapshot['received'])}</p>"
        "<div class='table-wrap'><table class='chain-table'><thead><tr><th colspan='8' class='opt-call'>CALL · CE</th>"
        "<th rowspan='2'>Strike</th><th colspan='8' class='opt-put'>PUT · PE</th></tr><tr>"
        + ("<th>LTP</th><th>Feed IV %</th><th>OI</th><th>Model IV %</th><th>Delta</th><th>Gamma</th><th>Theta/day</th><th>Vega/pt</th>" * 2)
        + "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div><p class='note'>Showing {len(rows)} of {len(strikes)} "
        "strikes. MP = max pain. ITM = in the money. Max OI is measured across the entire expiry. Totals use the full selected expiry. Model uses last price and spot at snapshot time, expiry 15:30 IST, ACT/365, zero interest and dividend yields. — = missing input, expired contract or no positive IV solution.</p></section>"
    )
    return _page(lang, title, body, active="chain")


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
    return _page(lang, t("sizing"), form + pre + note, active="sizing")


def paper_page(lang: str) -> str:
    t = lambda k: i18n_mod.t(k, lang)
    try:
        positions = paper_mod.list_positions()
        trades = paper_mod.list_trades()
        today = datetime.now(IST).date()
        summary = paper_mod.day_summary(today)
    except Exception as exc:  # noqa: BLE001
        return _page(lang, t("paper_trading"), f"<p>unavailable: {_esc(exc)}</p>", active="paper")
    pos_rows = "".join(
        f"<tr><td>{_esc(p.get('id', ''))}</td><td>{_esc(p.get('symbol', ''))}</td>"
        f"<td>{_opt(p.get('direction', ''))}</td><td>{_esc(p.get('qty', ''))}</td>"
        f"<td>{_esc(p.get('entry', ''))}</td><td>{_esc(p.get('stop', ''))}</td>"
        f"<td>{_esc(p.get('target', ''))}</td></tr>"
        for p in positions
    )
    tr_rows = "".join(
        f"<tr><td>{_esc(tr.get('id', ''))}</td><td>{_esc(tr.get('symbol', ''))}</td>"
        f"<td>{_opt(tr.get('direction', ''))}</td><td>{_esc(tr.get('exit_price', ''))}</td>"
        f"<td>{_esc(tr.get('realised_pnl', ''))}</td><td>{_esc(tr.get('reason', ''))}</td></tr>"
        for tr in trades[-20:]
    )
    # Exit check — fresh news against an open position (cached 5 min
    # per symbol). A severe headline says EXIT NOW, a mild one says
    # EXIT EARLY; no news against the position says HOLD.
    exit_rows = ""
    for p in positions:
        try:
            if poller_mod.is_running():
                news_result = poller_mod.get_news(p["symbol"]) or {"unavailable": True}
            else:
                news_result = _cached(
                    f"news:{p['symbol']}", 300,
                    lambda s=p["symbol"]: news_mod.check_symbol(s))
        except Exception:  # noqa: BLE001 - news failure never blocks the page
            news_result = {"unavailable": True}
        try:
            check = signal_mod.exit_signal(
                p["symbol"], p["direction"], p["entry"],
                news_result=news_result)
            exit_rows += (
                f"<tr><td>{_esc(p.get('id', ''))}</td>"
                f"<td>{_esc(p.get('symbol', ''))}</td>"
                f"<td>{_opt(p.get('direction', ''))}</td>"
                f"<td><b>{_esc(check['action'])}</b></td>"
                f"<td>{_esc(check['reason'])}</td></tr>"
            )
        except Exception as exc:  # noqa: BLE001 - a failed check never blocks the page
            exit_rows += (
                f"<tr><td>{_esc(p.get('id', ''))}</td>"
                f"<td>{_esc(p.get('symbol', ''))}</td>"
                f"<td>{_opt(p.get('direction', ''))}</td>"
                f"<td><b>HOLD</b></td>"
                f"<td>Exit check unavailable — {_esc(exc)}</td></tr>"
            )
    exit_html = (
        "<h3>Exit check — fresh news on open positions</h3>"
        "<div class='table-wrap'><table><tr><th>ID</th><th>Symbol</th><th>Dir</th>"
        "<th>Action</th><th>Why</th></tr>"
        f"{exit_rows or '<tr><td colspan=5>no open positions</td></tr>'}</table></div>"
    )
    body = (
        f"<p>Today: {summary.get('trades', 0)} trade(s), realised "
        f"₹{summary.get('realised_pnl', 0):,.2f}, risk taken "
        f"₹{summary.get('risk_taken', 0):,.2f}, flat: {summary.get('flat')}</p>"
        f"<h3>Open positions</h3><div class='table-wrap'><table><tr><th>ID</th><th>Symbol</th><th>Dir</th>"
        f"<th>Qty</th><th>Entry</th><th>Stop</th><th>Target</th></tr>{pos_rows or '<tr><td colspan=7>none</td></tr>'}</table></div>"
        f"{exit_html}"
        f"<h3>Closed trades</h3><div class='table-wrap'><table><tr><th>ID</th><th>Symbol</th><th>Dir</th>"
        f"<th>Exit</th><th>P&amp;L</th><th>Reason</th></tr>{tr_rows or '<tr><td colspan=6>none</td></tr>'}</table></div>"
    )
    return _page(lang, t("paper_trading"), body, active="paper")


def _api_flow() -> dict:
    """Smart Money Flow JSON — live engine first (warmed every 5 min),
    on-demand fetch (cached 5 min) only when the engine has nothing."""
    warmed = poller_mod.get_flow()
    if warmed is not None:
        return warmed
    if poller_mod.is_running():
        return {"series": [], "failed": [], "error": "Flow feed warming up"}
    try:
        return _cached("api:flow", 300, charts_mod.flow_series)
    except Exception as exc:  # noqa: BLE001
        return {"series": [], "failed": [], "error": str(exc)}


def _api_sectors() -> dict:
    """Sector map JSON — live engine first, same flow data, so no
    second round of fetches."""
    warmed = poller_mod.get_sectors()
    if warmed is not None:
        return warmed
    if poller_mod.is_running():
        return {"sectors": [], "failed": [], "error": "Sector feed warming up"}
    try:
        flow = _cached("api:flow", 300, charts_mod.flow_series)
        return charts_mod.sector_map(flow)
    except Exception as exc:  # noqa: BLE001
        return {"sectors": [], "failed": [], "error": str(exc)}


#: Index symbols for the live quote refresh (home page).
_QUOTE_SYMBOLS: tuple[tuple[str, str], ...] = (
    ("^NSEI", "NIFTY 50"),
    ("^NSEBANK", "BANK NIFTY"),
    ("^BSESN", "SENSEX"),
)


def _api_quotes() -> dict:
    """Live index quotes for the home-page refresh.

    The live engine's warmed quotes come first (NSE + fresh spark,
    in-memory, instant); only when the engine has nothing yet does
    this fall back to the on-demand fetch (cached 60s)."""
    warmed = poller_mod.get_quotes()
    if warmed is not None:
        return {"quotes": warmed,
                "as_of": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")}
    if poller_mod.is_running():
        return {"quotes": [], "error": "Index quotes warming up"}
    out = []
    for symbol, label in _QUOTE_SYMBOLS:
        try:
            q = _cached(f"quote:{symbol}", 60, lambda ys=symbol: data_mod.fetch_quote(ys))
            out.append(
                {
                    "symbol": symbol,
                    "label": label,
                    "price": q.get("price"),
                    "prev_close": q.get("prev_close"),
                    "change_pct": q.get("change_pct"),
                    "error": None,
                }
            )
        except Exception as exc:  # noqa: BLE001 - report per symbol, never fake
            out.append(
                {
                    "symbol": symbol,
                    "label": label,
                    "price": None,
                    "prev_close": None,
                    "change_pct": None,
                    "error": str(exc),
                }
            )
    return {"quotes": out, "as_of": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")}


def _api_intraday(query: dict) -> dict:
    """Intraday line/candle payload for one index.

    The live engine's warmed 5m bars come first (in-memory, instant);
    only when the engine has nothing yet does this fall back to the
    on-demand fetch (cached 60s)."""
    symbol = (query.get("symbol") or ["^NSEI"])[0]
    if symbol not in ("^NSEI", "^NSEBANK", "^BSESN"):
        return {"symbol": symbol, "line": [], "candles": [], "error": f"unknown symbol {symbol}"}
    warmed = poller_mod.get_intraday(symbol)
    if warmed is not None:
        return warmed
    if poller_mod.is_running():
        return {"symbol": symbol, "line": [], "candles": [], "error": "Intraday feed warming up"}
    try:
        return _cached(
            f"chart:{symbol}", 60, lambda: charts_mod.intraday_series(symbol)
        )
    except Exception as exc:  # noqa: BLE001
        return {"symbol": symbol, "line": [], "candles": [], "error": str(exc)}


def _api_orbchart(query: dict) -> dict:
    """Candlestick + overlays for the ORB hit of ``?symbol=`` (cached 60s)."""
    symbol = (query.get("symbol") or [""])[0].upper()
    try:
        result = _orb_scan()
    except Exception as exc:  # noqa: BLE001
        return {"symbol": symbol, "candles": [], "error": f"scan unavailable: {exc}"}
    picked = next(
        (h for h in result.get("hits", []) if h.get("symbol", "").upper() == symbol),
        None,
    )
    if picked is None:
        return {"symbol": symbol, "candles": [], "error": f"no ORB hit for {symbol}"}
    try:
        return _cached(
            f"orbchart:{picked['yahoo_symbol']}", 60,
            lambda: charts_mod.orb_chart(picked["yahoo_symbol"], picked["symbol"]),
        )
    except Exception as exc:  # noqa: BLE001
        return {"symbol": symbol, "candles": [], "error": str(exc)}


def _api_levelschart(query: dict) -> dict:
    """Candlestick + overlays for one index's levels (cached 60s)."""
    symbol = (query.get("symbol") or ["NIFTY"])[0].upper()
    yahoo_symbol = data_mod.INDICES.get(symbol)
    if yahoo_symbol is None:
        return {"symbol": symbol, "candles": [], "error": f"unknown index {symbol}"}
    try:
        return _cached(
            f"levelschart:{symbol}", 60,
            lambda: charts_mod.levels_chart(yahoo_symbol, symbol),
        )
    except Exception as exc:  # noqa: BLE001
        return {"symbol": symbol, "candles": [], "error": str(exc)}


def _api_timemachine() -> dict:
    """Monte Carlo + trade check + backtest — live engine first
    (warmed every 10 min), on-demand run (cached 10 min) only when
    the engine has nothing. Returns the data plus a pre-rendered HTML
    body for the lazy page load."""
    data = poller_mod.get_timemachine()
    if data is None:
        if poller_mod.is_running():
            return {"error": "Background scan warming up", "html": ""}
        try:
            data = _cached("timemachine", 600, timecheck_mod.collect_timemachine)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}
    out = dict(data)
    out["html"] = _timemachine_body(data)
    return out


def _api_timemachine_check(query: dict) -> dict:
    """User's own trade through the Time Machine — the form's endpoint.

    GET params: symbol, entry, stop, target, direction (CALL/PUT or
    long/short). Read-only: reuses timecheck.check_trade, which replays
    the symbol's own 5m bars and runs the same trade check — zero
    effect on the robot, signals, scans or poller. The check is cached
    5 minutes per exact input so a double submit does not re-run
    10,000 replays. Always returns a 200 JSON with ok True/False —
    never a 500 on bad input."""
    symbol = (query.get("symbol") or [""])[0]
    entry = (query.get("entry") or [""])[0]
    stop = (query.get("stop") or [""])[0]
    target = (query.get("target") or [""])[0]
    direction = (query.get("direction") or [""])[0]
    if not symbol or entry == "" or stop == "" or target == "" or not direction:
        return {"ok": False, "error": "symbol, entry, stop, target aur direction — sab bharo."}
    try:
        result = _cached(
            f"tmcheck:{symbol}:{entry}:{stop}:{target}:{direction}", 300,
            lambda: timecheck_mod.check_trade(symbol, entry, stop, target, direction))
    except Exception as exc:  # noqa: BLE001 - bad input must not 500
        return {"ok": False, "error": str(exc)}
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "check nahi ho paya")}
    out = dict(result)
    out["html"] = _tm_check_result_html(result)
    return out


def _api_orb(query: dict, lang: str) -> dict:
    """ORB scan table HTML fragment (the scan is cached 2 min — it
    fetches 39 symbols). ``?limit=`` caps the rows (home uses 10).
    ``top_symbol`` is the first hit, for the lazy candle chart."""
    try:
        limit = int((query.get("limit") or ["50"])[0])
    except (TypeError, ValueError):
        limit = 50
    try:
        result = _orb_scan()
    except Exception as exc:  # noqa: BLE001
        return {"html": "", "error": f"scan unavailable: {exc}"}
    hits = result.get("hits", [])
    return {
        "html": _orb_table_html(result, lang, limit=limit),
        "top_symbol": hits[0].get("symbol", "") if hits else "",
    }


def _api_orbbreakdown(query: dict, lang: str) -> dict:
    """Signal breakdown HTML fragment for one ORB hit."""
    symbol = (query.get("symbol") or [""])[0].upper()
    try:
        result = _orb_scan()
    except Exception as exc:  # noqa: BLE001
        return {"html": "", "error": f"scan unavailable: {exc}"}
    picked = next(
        (h for h in result.get("hits", []) if h.get("symbol", "").upper() == symbol),
        None,
    )
    if picked is None:
        return {"html": "<div class='card'><h3>Signal breakdown</h3><p>No ORB hit for this symbol.</p></div>"}
    stop_mid = (picked["range_high"] + picked["range_low"]) / 2.0
    _tp1, target_2r, _tp3 = _tp123(picked["direction"], picked["close"], stop_mid)
    return {
        "html": _breakdown_for(
            lang, picked["symbol"], picked["direction"], picked["close"],
            stop_mid, target_2r,
            support=picked["range_low"], resistance=picked["range_high"],
            trigger_time=picked.get("bar_time"),
            yahoo_symbol=picked["yahoo_symbol"],
        )
    }


def _api_levels(query: dict, lang: str) -> dict:
    """Levels table HTML fragment — live engine first (warmed every
    2 min), on-demand scan (cached 60s) only when the engine has
    nothing. ``top_symbol`` is the first row, for the lazy candle chart."""
    warmed = poller_mod.get_levels()
    if warmed is not None:
        rows = warmed.get("levels", [])
        return {
            "html": _levels_table_html(warmed, lang),
            "top_symbol": rows[0].get("symbol", "") if rows else "",
        }
    if poller_mod.is_running():
        return {"html": "", "error": "Levels scan warming up"}
    try:
        result = _cached("levels", 60, levels_mod.scan_levels)
    except Exception as exc:  # noqa: BLE001
        return {"html": "", "error": f"levels unavailable: {exc}"}
    rows = result.get("levels", [])
    return {
        "html": _levels_table_html(result, lang),
        "top_symbol": rows[0].get("symbol", "") if rows else "",
    }


def _api_levelsbreakdown(query: dict, lang: str) -> dict:
    """Signal breakdown HTML fragment for one index's levels."""
    symbol = (query.get("symbol") or ["NIFTY"])[0].upper()
    try:
        result = poller_mod.get_levels()
        if result is None:
            if poller_mod.is_running():
                return {"html": "", "error": "Levels scan warming up"}
            result = _cached("levels", 60, levels_mod.scan_levels)
    except Exception as exc:  # noqa: BLE001
        return {"html": "", "error": f"levels unavailable: {exc}"}
    rows = result.get("levels", [])
    picked = next(
        (r for r in rows if r.get("symbol", "").upper() == symbol),
        rows[0] if rows else None,
    )
    if picked is None or not picked.get("signal") or not picked.get("entry") \
            or not picked.get("stop") or not picked.get("target"):
        return {"html": "<div class='card'><h3>Signal breakdown</h3><p>No signal — no entry to judge.</p></div>"}
    yahoo_symbol = data_mod.INDICES.get(picked["symbol"], "^NSEI")
    try:
        bars = _cached(
            f"signalbars:{yahoo_symbol}", 60,
            lambda: data_mod.fetch_intraday_bars(yahoo_symbol, interval="5m"))
        hit = scanner_mod.orb_hit(bars) if bars is not None else None
        trigger_time = hit.get("bar_time") if hit else None
    except Exception:  # noqa: BLE001 - volume degrades to neutral
        trigger_time = None
    return {
        "html": _breakdown_for(
            lang, picked["symbol"], picked["signal"], picked["entry"],
            picked["stop"], picked["target"],
            support=picked.get("nearest_support"),
            resistance=picked.get("nearest_resistance"),
            trigger_time=trigger_time, yahoo_symbol=yahoo_symbol,
        )
    }


def _api_news(query: dict, lang: str) -> dict:
    """News feed HTML fragment — the live engine's warmed news check
    first, so the home-page refresh never waits on a fresh fetch."""
    try:
        return {"html": _news_feed_html(
            lang, news=poller_mod.get_news("NIFTY"))}
    except Exception as exc:  # noqa: BLE001
        return {"html": "", "error": str(exc)}


def _robot_html() -> str:
    """Robot status as a trade panel — the bot's own entry/exit (one
    entry, one exit) from the fresh scans, plus what the live engine
    is doing right now.

    Every number comes from the engine's in-memory state (instant,
    never a fetch): last scan times, symbols scanned, ORB hits, news
    check time, Time Machine replays, and the best setup the fresh
    scans found, with entry/SL/TP1/TP2/TP3. When the engine has not
    scanned yet, the card says it is warming up — never a fake status.
    """
    st = poller_mod.status()
    ticks = st.get("ticks", {})

    def _clock(stamp: str) -> str:
        # "2026-11-26 14:30:00 IST" -> "14:30:00"
        return stamp[11:19] if len(stamp) >= 19 else (stamp or "—")

    market = "OPEN" if st.get("market_open") else "closed (slow polls)"
    live_badge = ("<span class='pill'>Background job updated</span>"
                  if st.get("fresh") else "<span>warming up…</span>")
    scans = (
        f"Last scans — indices {_clock(ticks.get('indices', ''))} · "
        f"spark {_clock(ticks.get('spark_fast', ''))} · intraday "
        f"{_clock(ticks.get('intraday', ''))} · flow "
        f"{_clock(ticks.get('flow', ''))} · news "
        f"{_clock(ticks.get('news', ''))} · scans "
        f"{_clock(ticks.get('scans', ''))} · Time Machine "
        f"{_clock(ticks.get('timemachine', ''))}"
    )
    counts = (
        f"{st.get('symbols_scanned', 0)} symbols scanned · "
        f"{st.get('spark_symbols', 0)} live spark feeds · "
        f"{st.get('orb_hits', 0)} ORB hit(s) now · news checked "
        f"{_clock(st.get('news_checked_at', ''))} · Time Machine "
        f"{st.get('timemachine_replays', 0):,} replays"
    )
    backoff = ""
    if st.get("backoff_until"):
        backoff = (
            f"<p class='muted'>Rate-limit backoff until "
            f"{_esc(st['backoff_until'])} — showing last good data.</p>"
        )
    elif st.get("errors"):
        backoff = (
            f"<p class='muted'>{st['errors']} fetch error(s) so far — "
            f"last good data kept.</p>"
        )

    # Best setup from the fresh scans: a levels signal first, else
    # the first ORB hit. Entry/SL/TPs are the robot's own numbers.
    best = ""
    levels = poller_mod.get_levels()
    picked = None
    if levels:
        for row in levels.get("levels", []) or []:
            if row.get("signal") and row.get("entry") and row.get("stop"):
                picked = (row.get("symbol"), row.get("signal"),
                          row.get("entry"), row.get("stop"))
                break
    if picked is None:
        orb = poller_mod.get_orb()
        if orb:
            for hit in orb.get("hits", []) or []:
                try:
                    stop = (float(hit["range_high"])
                            + float(hit["range_low"])) / 2.0
                    picked = (hit.get("symbol"), hit.get("direction"),
                              float(hit["close"]), stop)
                    break
                except (KeyError, TypeError, ValueError):
                    continue
    if picked:
        symbol, direction, entry, stop = picked
        tp1, tp2, tp3 = _tp123(str(direction), float(entry), float(stop))
        try:
            risk = abs(float(entry) - float(stop))
            rr_txt = (f"1:{abs(float(tp2) - float(entry)) / risk:.1f}"
                      if risk > 0 else "—")
        except (TypeError, ValueError):
            rr_txt = "—"
        panel = (
            f"<div class='tpanel'>"
            f"<div class='tt'><span>AAJ KA BEST SETUP</span>"
            f"<span>{live_badge}</span></div>"
            f"<div class='tn'>{_esc(symbol)} {_opt(direction)} "
            f"<span class='muted'>fresh scan {_clock(ticks.get('scans', ''))}</span></div>"
            f"<div class='ts'><div>ENTRY<b>{float(entry):,.2f}</b></div>"
            f"<div>STOP<b>{float(stop):,.2f}</b></div>"
            f"<div>R:R<b>{rr_txt}</b></div></div>"
            f"<div class='tr'><span>TP1 — 1R</span><b>{tp1:,.2f}</b></div>"
            f"<div class='tr'><span>TP2 — 2R</span><b>{tp2:,.2f}</b></div>"
            f"<div class='tr'><span>TP3 — 3R</span><b>{tp3:,.2f}</b></div>"
            f"{_rr_bar_html(entry, stop, tp2)}"
            f"<div class='st sig'><b>SIGNAL</b> — entry {float(entry):,.2f}, "
            f"exit at SL {float(stop):,.2f} or TP1/TP2/TP3. One entry, one exit.</div>"
            f"</div>"
        )
    else:
        panel = (
            f"<div class='tpanel'>"
            f"<div class='tt'><span>AAJ KA BEST SETUP</span>"
            f"<span>{live_badge}</span></div>"
            f"<div class='st none'><b>WATCHING</b> — no ORB signal right now, "
            f"robot is watching.</div></div>"
        )
    return (
        f"<div class='card'><div class='ml-h'><h3>BOT</h3>"
        f"<span class='sub'>news check + 10,000 replays + volume — "
        f"entry/exit isse banta hai</span><span class='sp'></span>"
        f"{live_badge}</div>"
        f"<p class='muted'>Market {market} · {counts}</p>"
        f"{panel}<p class='muted'>{scans}</p>{backoff}</div>"
    )


def _api_robot() -> dict:
    """Robot card HTML fragment for the home-page refresh."""
    try:
        return {"html": _robot_html()}
    except Exception as exc:  # noqa: BLE001
        return {"html": "", "error": str(exc)}


def _api_live(query: dict) -> dict:
    """Live tick for one index: latest price + 1-minute closes.

    Served from the live engine's spark state (in-memory, instant —
    the indices spark every 2 s). The browser's line charts call this
    every 2 s and update the last point in place, so the line moves
    while you watch — no page reload, no 60 s wait. When the engine
    has not had a spark tick yet, this says so honestly instead of
    faking a price."""
    symbol = (query.get("symbol") or ["^NSEI"])[0]
    if symbol not in ("^NSEI", "^NSEBANK", "^BSESN"):
        return {"symbol": symbol, "ltp": None, "line": [],
                "error": f"unknown symbol {symbol}"}
    entry = poller_mod.get_spark(symbol)
    if entry is None:
        return {"symbol": symbol, "ltp": None, "line": [],
                "error": "live feed warming up — first spark in a few seconds"}
    closes = entry.get("closes", []) or []
    line = [{"time": int(c["t"]), "value": float(c["c"])}
            for c in closes[-400:]]
    ltp = entry.get("ltp")
    # Same pinned previous close as /api/quotes — never Yahoo's own
    # chartPreviousClose on its own (it disagreed with NSE on 26 Nov
    # 2026: 25,349.70 vs 25,819.25 for NIFTY).
    prev = poller_mod.get_prev_close(symbol)
    if prev is None:
        prev = entry.get("prev")
    pct = round((ltp / prev - 1) * 100, 2) if ltp and prev else None
    return {"symbol": symbol, "ltp": ltp, "prev": prev, "change_pct": pct,
            "time": entry.get("time") or 0, "line": line, "error": None}


def _api_poller() -> dict:
    """Live-engine status (in-memory, instant — never fetches)."""
    return poller_mod.status()


def _levels_mini_html(result: dict) -> str:
    """Compact levels summary for home: symbol / price / support /
    resistance / signal (5 columns, wrapped like every table)."""
    rows = result.get("levels", [])[:3]

    def _fmt(value) -> str:
        return f"{value:,.2f}" if value is not None else "—"

    body_rows = "".join(
        f"<tr><td>{_esc(r.get('symbol', ''))}</td><td>{_fmt(r.get('price'))}</td>"
        f"<td>{_fmt(r.get('nearest_support'))}</td>"
        f"<td>{_fmt(r.get('nearest_resistance'))}</td>"
        f"<td>{_opt(r.get('signal')) if r.get('signal') else 'None'}</td></tr>"
        for r in rows
    )
    if not rows:
        return (
            "<div class='card'><h3>Levels — indices</h3>"
            "<p class='muted'>Levels warming up — first scan in progress…</p></div>"
        )
    return (
        f"<div class='card'><h3>Levels — indices</h3>"
        f"<p class='muted'>As of {_esc(result.get('run_at_ist', ''))} · "
        f"<a href='/levels'>Full levels page</a></p>"
        f"<div class='table-wrap'><table>"
        f"<tr><th>Symbol</th><th>Price</th><th>Support</th>"
        f"<th>Resistance</th><th>Signal</th></tr>{body_rows}</table></div></div>"
    )


def _api_levelsmini() -> dict:
    """Levels mini-summary HTML fragment for home."""
    result = poller_mod.get_levels()
    if result is None:
        if poller_mod.is_running():
            return {"error": "Background scan warming up", "html": ""}
        try:
            result = _cached("levels", 60, levels_mod.scan_levels)
        except Exception as exc:  # noqa: BLE001
            return {"html": "", "error": f"levels unavailable: {exc}"}
    return {"html": _levels_mini_html(result)}


def _tm_mini_html(data: dict) -> str:
    """Compact Time Machine summary for home: today's best setup +
    Monte Carlo target-first % (10,000 replays)."""
    setup = data.get("setup")
    mc = data.get("monte_carlo", {}) or {}
    if not setup and not mc.get("available"):
        return (
            "<div class='card'><h3>Time Machine — today</h3>"
            "<p class='muted'>Time Machine warming up — first 10,000-replay "
            "check in progress…</p></div>"
        )

    def _fmt(value) -> str:
        return f"{value:,.2f}" if value is not None else "—"

    lines = ""
    if setup:
        lines += (
            f"<p><b>{_esc(setup.get('symbol', ''))} "
            f"{_opt(setup.get('signal', ''))}</b> — entry "
            f"{_fmt(setup.get('entry'))}, stop {_fmt(setup.get('stop'))}, "
            f"target {_fmt(setup.get('target'))}.</p>"
        )
    if mc.get("available"):
        lines += (
            f"<p>{mc.get('n', 0):,} replays — target first "
            f"{mc.get('target_first_pct', 0):.1f}% · stop first "
            f"{mc.get('stop_first_pct', 0):.1f}% · average "
            f"{mc.get('avg_r', 0):+.2f}R · "
            f"<a href='/timemachine'>Full Time Machine</a></p>"
        )
    return f"<div class='card'><h3>Time Machine — today</h3>{lines}</div>"


def _api_tmmini() -> dict:
    """Time Machine mini-summary HTML fragment for home."""
    data = poller_mod.get_timemachine()
    if data is None:
        if poller_mod.is_running():
            return {"error": "Background scan warming up", "html": ""}
        try:
            data = _cached("timemachine", 600, timecheck_mod.collect_timemachine)
        except Exception as exc:  # noqa: BLE001
            return {"html": "", "error": str(exc)}
    return {"html": _tm_mini_html(data)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the console quiet
        pass

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/api/history":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8 * 1024 * 1024:
                raise ValueError("CSV must be between 1 byte and 8 MiB.")
            q = parse_qs(url.query)
            args = [q.get(k, [""])[0] for k in ("start", "end", "fee", "slip", "qty")]
            result = range_replay.replay(self.rfile.read(length).decode("utf-8"), *args, strategy=q.get("strategy", ["orb15"])[0])
        except (ValueError, UnicodeError) as exc:
            result = {"error": str(exc)}
        self._send_json(result)

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        lang = _lang(query)
        try:
            details = {
                "/api/orbchart": lambda: _api_orbchart(query),
                "/api/levelschart": lambda: _api_levelschart(query),
                "/api/timemachine-check": lambda: _api_timemachine_check(query),
                "/api/orbbreakdown": lambda: _api_orbbreakdown(query, lang),
                "/api/levelsbreakdown": lambda: _api_levelsbreakdown(query, lang),
                "/api/skills": lambda: _api_skills(),
                "/api/stock": lambda: _api_stock(query),
            }
            build = details.get(parsed.path)
            if build:
                if self.headers.get("Prefer") == "respond-async":
                    key = (parsed.path, tuple(sorted((k,v[0]) for k,v in query.items())))
                    status, result = _detail_jobs.read(key, build)
                    self._send_json(result, status)
                else:
                    self._send_json(build())
                return
            if parsed.path == "/healthz":
                self._send_json({"status": "ok"})
                return
            if parsed.path == "/api/flow":
                self._send_json(_api_flow())
                return
            if parsed.path == "/api/sectors":
                self._send_json(_api_sectors())
                return
            if parsed.path == "/api/quotes":
                self._send_json(_api_quotes())
                return
            if parsed.path == "/api/intraday":
                self._send_json(_api_intraday(query))
                return
            if parsed.path == "/api/timemachine":
                self._send_json(_api_timemachine())
                return
            if parsed.path == "/api/orb":
                self._send_json(_api_orb(query, lang))
                return
            if parsed.path == "/api/levels":
                self._send_json(_api_levels(query, lang))
                return
            if parsed.path == "/api/news":
                self._send_json(_api_news(query, lang))
                return
            if parsed.path == "/api/live":
                self._send_json(_api_live(query))
                return
            if parsed.path == "/api/poller":
                self._send_json(_api_poller())
                return
            if parsed.path == "/api/robot":
                self._send_json(_api_robot())
                return
            if parsed.path == "/api/levelsmini":
                self._send_json(_api_levelsmini())
                return
            if parsed.path == "/api/tmmini":
                self._send_json(_api_tmmini())
                return
            if parsed.path.startswith("/static/"):
                served = _static_file(parsed.path[len("/static/"):])
                if served is None:
                    self._send(_page(lang, "Not found", "<p>Unknown file.</p>"), 404)
                    return
                data, ctype = served
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(data)
                return
            if parsed.path in ("/", ""):
                body = home(lang)
            elif parsed.path == "/orb":
                body = orb_page(lang, query)
            elif parsed.path == "/levels":
                body = levels_page(lang, query)
            elif parsed.path == "/stock":
                body = stock_page(lang, query)
            elif parsed.path == "/sectors":
                body = sectors_page(lang)
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
        except (BrokenPipeError, ConnectionResetError):
            return  # The browser navigated away while a section was loading.
        except Exception as exc:  # noqa: BLE001 - never crash the server on a page
            self._send(_page(lang, "Error", f"<p>Something went wrong: {_esc(exc)}</p>"), 500)

    def _send_json(self, payload: dict, status: int = 200):
        try:
            data = json.dumps(payload, default=str).encode("utf-8")
        except Exception as exc:  # noqa: BLE001
            data = json.dumps({"error": str(exc)}).encode("utf-8")
        self.send_response(status)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send(self, body: str, status: int):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    try:
        poller_mod.start()
    except Exception:  # noqa: BLE001 - the server must start even if the engine cannot
        pass
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Bhanu Indian Day Trader running at http://localhost:{PORT} (paper trading only)")
    server.serve_forever()


if __name__ == "__main__":
    main()
