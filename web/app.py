"""Bhanu Indian Day Trader — web dashboard.

Stdlib-only HTTP server (no framework). Pages:
  /         home — index quotes + intraday charts, Smart Money Flow,
            sector map, news feed, ORB hits (charts on top, tables below)
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

Pages render INSTANTLY from local data only: every network fetch
(quotes, charts, scans, news, Time Machine) happens lazily from
/api/* after the page, with a "loading" state until the data arrives.
Quotes/charts are cached 60s, flow/sector 5 min, the Time Machine
10 min — repeat loads show instantly. When a source is unavailable
the page says so plainly — no fake data is ever shown.

Charts render with TradingView Lightweight Charts, bundled locally
(web/static/lightweight-charts.js); when the library cannot load, a
built-in SVG line/candlestick chart is drawn instead — a chart box
is never left blank.
Chart pages auto-refresh every 60 seconds (LIVE indicator, last-update
time) — charts update in place, no page reload, no flicker. A failed
refresh keeps the old data and says "update failed, retrying…".
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
from bhanu import options as options_mod
from bhanu import paper as paper_mod
from bhanu import scanner as scanner_mod
from bhanu import signal as signal_mod
from bhanu import sizing as sizing_mod
from bhanu import skills as skills_mod
from bhanu import timecheck as timecheck_mod
from bhanu import volume as volume_mod
from bhanu import news as news_mod

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("PORT", "8810"))

#: Auto-refresh interval for chart pages (milliseconds). Every 60s each
#: registered chart re-fetches its data and updates in place — no page
#: reload. Server-side caches are 30–600s, so a tick usually returns
#: fresh-enough data without hammering Yahoo/NSE.
LIVE_REFRESH_MS = 60_000

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
          live: bool = False) -> str:
    """Page shell. ``live=True`` adds the LIVE indicator + auto-refresh
    engine (chart pages only — static pages never claim to be live)."""
    t = lambda k: i18n_mod.t(k, lang)
    live_html = ""
    if live:
        live_html = (
            "<p class='muted' id='bhanu-live-indicator' "
            "title='Charts refresh every 60 seconds — no page reload'>"
            "● LIVE — last update --:--:--</p>"
        )
        scripts = scripts + _live_js()
    nav = (
        f'<a href="/?lang={lang}">{t("home")}</a> | '
        f'<a href="/orb?lang={lang}">{t("orb_scanner")}</a> | '
        f'<a href="/levels?lang={lang}">{t("levels")}</a> | '
        f'<a href="/sectors?lang={lang}">Sectors</a> | '
        f'<a href="/chain?lang={lang}">{t("option_chain")}</a> | '
        f'<a href="/sizing?lang={lang}">{t("sizing")}</a> | '
        f'<a href="/paper?lang={lang}">{t("paper_trading")}</a> | '
        f'<a href="/skills?lang={lang}">{t("skills")}</a> | '
        f'<a href="/timemachine?lang={lang}">{t("timemachine")}</a> | '
        f'<a href="?lang=en">EN</a> <a href="?lang=hi">HI</a>'
    )
    return f"""<!DOCTYPE html>
<html lang="en" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} — Bhanu Indian Day Trader</title>
<style>
:root{{--bg:#f7f8fa;--card:#ffffff;--text:#1a1a1a;--muted:#666;--border:#ddd;
--accent:#2962ff;--up:#26a69a;--down:#ef5350;--th:#f0f0f0;--grid:#eee}}
html[data-theme="dark"]{{--bg:#121417;--card:#1c1f24;--text:#e8e8e8;--muted:#9aa0a6;
--border:#333;--accent:#7aa2ff;--up:#26a69a;--down:#ef5350;--th:#24272c;--grid:#2a2d33}}
body{{font-family:system-ui,sans-serif;max-width:1020px;margin:24px auto;padding:0 16px;
line-height:1.5;background:var(--bg);color:var(--text)}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid var(--border);
padding:6px 8px;text-align:left}}th{{background:var(--th)}}
.up{{color:var(--up)}} .down{{color:var(--down)}} .muted{{color:var(--muted)}}
nav{{margin-bottom:16px}} nav a{{color:var(--accent)}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:12px;
padding:16px;margin:16px 0;box-shadow:0 1px 3px rgba(0,0,0,0.06)}}
.card h3{{margin-top:0}}
.chart-wrap{{position:relative;width:100%}}
.chart-box{{width:100%;height:300px}}
.chart-box.tall{{height:420px}}
.news-item{{border-left:3px solid var(--accent);padding:8px 12px;margin:8px 0;
background:var(--card)}}
.theme-btn{{float:right;background:var(--card);color:var(--text);
border:1px solid var(--border);border-radius:8px;padding:4px 10px;cursor:pointer}}
.sector-bar{{height:10px;border-radius:5px;background:var(--up)}}
.sector-bar.down{{background:var(--down)}}
.index-cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin:16px 0}}
.index-card{{background:var(--card);border:1px solid var(--border);border-radius:12px;
padding:16px;box-shadow:0 1px 3px rgba(0,0,0,0.06)}}
.index-name{{font-size:13px;font-weight:600;letter-spacing:.04em;color:var(--muted);text-transform:uppercase}}
.index-price{{font-size:30px;font-weight:800;line-height:1.2}}
.index-change{{font-size:18px;font-weight:700}}
.index-prev{{font-size:12px;color:var(--muted)}}
.loading{{color:var(--muted);font-style:italic}}
</style>{head_extra}</head>
<body><button class="theme-btn" onclick="bhanuTheme()">🌙 / ☀️</button>
<h1>Bhanu Indian Day Trader</h1><nav>{nav}</nav>
<h2>{_esc(title)}</h2>{live_html}{body}
<p class="muted">Paper trading only — no real orders. Educational software, not investment advice.</p>
<script>
function bhanuTheme(){{
  var h=document.documentElement;
  var next=h.getAttribute('data-theme')==='dark'?'light':'dark';
  h.setAttribute('data-theme',next);
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
    if name != CHART_LOCAL:
        return None
    path = ROOT / "web" / "static" / name
    try:
        return path.read_bytes(), "application/javascript; charset=utf-8"
    except OSError:
        return None


def _chart_lib_tag() -> str:
    """Script tag for the chart library — local bundle first, CDN fallback."""
    if (ROOT / "web" / "static" / CHART_LOCAL).exists():
        return '<script src="/static/lightweight-charts.js"></script>'
    return f'<script src="{CHART_CDN}"></script>'


def _live_js() -> str:
    """Auto-refresh engine for chart pages.

    Every LIVE_REFRESH_MS each registered refresher (a function returning
    a Promise) re-fetches its chart data and updates the chart in place —
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
function bhanuLiveSet(state){
  var el=document.getElementById('bhanu-live-indicator');
  if(!el){return;}
  if(state==='ok'){
    var t=bhanuLiveTime();
    el.textContent='● LIVE — last update '+t;
    el.setAttribute('data-last',t);
  }else if(state==='fail'){
    var last=el.getAttribute('data-last')||'—';
    el.textContent='● LIVE — update failed, retrying… (last good '+last+')';
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
  ind.textContent='● LIVE — loading…';
  setInterval(bhanuLiveTick, BHANU_REFRESH_MS);
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
window.bhanuCandles = window.bhanuCandles || {};
function bhanuDark(){return document.documentElement.getAttribute('data-theme')==='dark';}
function bhanuChartOpts(h){
  var dark=bhanuDark();
  return {width:0,height:h||300,
    layout:{background:{color:'transparent'},textColor:dark?'#c9c9c9':'#333'},
    grid:{vertLines:{color:dark?'#2a2d33':'#eee'},horzLines:{color:dark?'#2a2d33':'#eee'}},
    timeScale:{timeVisible:true,secondsVisible:false}};
}
function bhanuUnavailable(el,why){
  el.innerHTML='<p class="muted">Chart unavailable — '+why+'</p>';
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
  el.innerHTML='<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:auto" role="img" aria-label="Price chart (simple view)"><path d="'+d+'" fill="none" stroke="'+(color||'#2962ff')+'" stroke-width="2"/><circle cx="'+X(points.length-1)+'" cy="'+Y(last)+'" r="4" fill="'+(color||'#2962ff')+'"/><text x="'+(W-8)+'" y="18" text-anchor="end" font-size="13" font-weight="bold" fill="'+(bhanuDark()?'#fff':'#111')+'">'+last.toLocaleString('en-IN')+'</text></svg><p class="muted">Simple chart view — full chart library could not load.</p>';
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
    var col=c.close>=c.open?'#26a69a':'#ef5350';
    s+='<line x1="'+(x+w/2).toFixed(1)+'" y1="'+Y(c.high).toFixed(1)+'" x2="'+(x+w/2).toFixed(1)+'" y2="'+Y(c.low).toFixed(1)+'" stroke="'+col+'" stroke-width="1"/>';
    s+='<rect x="'+x.toFixed(1)+'" y="'+Y(Math.max(c.open,c.close)).toFixed(1)+'" width="'+w.toFixed(1)+'" height="'+Math.max(1,Math.abs(Y(c.open)-Y(c.close))).toFixed(1)+'" fill="'+col+'"/>';
  });
  s+='</svg><p class="muted">Simple chart view — full chart library could not load.</p>';
  el.innerHTML=s;
}
function bhanuSvgFlow(el, series){
  if(!series||!series.length){bhanuUnavailable(el,'no flow data');return;}
  var W=900,H=300,pad=8;
  var mn=0,mx=0;
  series.forEach(function(s){s.line.forEach(function(p){if(p.value<mn){mn=p.value;}if(p.value>mx){mx=p.value;}});});
  if(mx===mn){mx=mn+1;}
  var maxLen=Math.max.apply(null,series.map(function(s){return s.line.length;}));
  function X(i){return pad+i/(maxLen-1||1)*(W-2*pad);}
  function Y(v){return H-pad-(v-mn)/(mx-mn)*(H-2*pad);}
  var s='<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:auto" role="img" aria-label="Smart Money Flow (simple view)">';
  series.forEach(function(ser){
    var col=ser.is_index?'#2962ff':(ser.up?'#26a69a':'#ef5350');
    var d=ser.line.map(function(p,i){return (i?'L':'M')+X(i).toFixed(1)+' '+Y(p.value).toFixed(1);}).join(' ');
    s+='<path d="'+d+'" fill="none" stroke="'+col+'" stroke-width="'+(ser.is_index?2:1)+'" opacity="0.7"/>';
  });
  s+='</svg><p class="muted">Simple chart view — full chart library could not load.</p>';
  el.innerHTML=s;
}
function bhanuLineRender(id, p, symbol){
  var el=document.getElementById(id);if(!el)return;
  if(!p||!p.line||p.line.length===0){bhanuUnavailable(el,p&&p.error?p.error:'no price data');return;}
  var fetchLine=function(){
    return fetch('/api/intraday?symbol='+encodeURIComponent(symbol)).then(function(r){
      if(!r.ok){throw new Error('intraday '+r.status);}return r.json();
    }).then(function(p2){
      if(!p2||p2.error||!p2.line||!p2.line.length){throw new Error(p2&&p2.error?p2.error:'no line data');}
      return p2;
    });
  };
  if(typeof LightweightCharts==='undefined'){
    bhanuSvgLine(el,p.line,p.color||'#2962ff');
    if(symbol){window.bhanuRefreshers.push(function(){return fetchLine().then(function(p2){bhanuSvgLine(el,p2.line,p.color||'#2962ff');});});}
    return;
  }
  var o=bhanuChartOpts(260);o.width=el.clientWidth||900;
  var ch=LightweightCharts.createChart(el,o);
  var s=ch.addLineSeries({color:p.color||'#2962ff',lineWidth:2});
  s.setData(p.line);ch.timeScale().fitContent();
  window.addEventListener('resize',function(){ch.applyOptions({width:el.clientWidth||900});});
  if(symbol){
    window.bhanuRefreshers.push(function(){return fetchLine().then(function(p2){s.setData(p2.line);});});
  }
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
  fetch('/api/intraday?symbol='+encodeURIComponent(symbol)).then(function(r){
    if(!r.ok){throw new Error('intraday '+r.status);}return r.json();
  }).then(function(p){
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
  var el=document.getElementById(id);if(!el)return false;
  if(typeof LightweightCharts==='undefined'){
    bhanuSvgFlow(el,p.series);
    var leg0=document.getElementById(id+'-legend');
    var up0=0,down0=0;
    p.series.forEach(function(s){if(s.up){up0++;}else{down0++;}});
    if(leg0){leg0.textContent=up0+' up / '+down0+' down — each line is one stock\\'s intraday % change (NIFTY = blue). Failed: '+(p.failed?p.failed.length:0)+'. As of '+p.as_of+'.';}
    return true;
  }
  var st=window.bhanuFlow=window.bhanuFlow||{};
  if(!st.chart){
    el.innerHTML='';
    var o=bhanuChartOpts(340);o.width=el.clientWidth||900;
    st.chart=LightweightCharts.createChart(el,o);
    st.series={};
    window.addEventListener('resize',function(){st.chart.applyOptions({width:el.clientWidth||900});});
  }
  var up=0,down=0;
  p.series.forEach(function(s){
    if(s.up){up++;}else{down++;}
    var col=s.is_index?'#2962ff':(s.up?'#26a69a':'#ef5350');
    var ls=st.series[s.symbol];
    if(!ls){
      ls=st.chart.addLineSeries({color:col,lineWidth:s.is_index?3:1,
        priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});
      st.series[s.symbol]=ls;
    }
    ls.setData(s.line);
  });
  st.chart.timeScale().fitContent();
  var leg=document.getElementById(id+'-legend');
  if(leg){leg.textContent=up+' up / '+down+' down — each line is one stock\\'s intraday % change (NIFTY = blue). Failed: '+(p.failed?p.failed.length:0)+'. As of '+p.as_of+'.';}
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
  var el=document.getElementById(id);if(!el)return false;
  var W=620,H=340,pad=30;
  var rels=p.sectors.map(function(s){return s.rel_pct===null?0:s.rel_pct;});
  var chs=p.sectors.map(function(s){return s.change_pct;});
  var x0=Math.min.apply(null,rels.concat([0]))-0.2,x1=Math.max.apply(null,rels.concat([0]))+0.2;
  var y0=Math.min.apply(null,chs.concat([0]))-0.2,y1=Math.max.apply(null,chs.concat([0]))+0.2;
  var maxV=Math.max.apply(null,p.sectors.map(function(s){return s.traded_value||1;}));
  function X(v){return pad+(v-x0)/(x1-x0)*(W-2*pad);}
  function Y(v){return H-pad-(v-y0)/(y1-y0)*(H-2*pad);}
  var svg='<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:auto" role="img" aria-label="Sector map">';
  svg+='<line x1="'+X(0)+'" y1="'+pad+'" x2="'+X(0)+'" y2="'+(H-pad)+'" stroke="#999" stroke-dasharray="4 4"/>';
  svg+='<line x1="'+pad+'" y1="'+Y(0)+'" x2="'+(W-pad)+'" y2="'+Y(0)+'" stroke="#999" stroke-dasharray="4 4"/>';
  p.sectors.forEach(function(s){
    var r=18+42*Math.sqrt((s.traded_value||1)/maxV);
    var col=s.up?'#26a69a':'#ef5350';
    svg+='<circle cx="'+X(s.rel_pct===null?0:s.rel_pct).toFixed(1)+'" cy="'+Y(s.change_pct).toFixed(1)+'" r="'+r.toFixed(1)+'" fill="'+col+'" fill-opacity="0.55" stroke="'+col+'" stroke-width="2"/>';
    svg+='<text x="'+X(s.rel_pct===null?0:s.rel_pct).toFixed(1)+'" y="'+(Y(s.change_pct)-4).toFixed(1)+'" text-anchor="middle" font-size="12" font-weight="bold" fill="'+(bhanuDark()?'#fff':'#111')+'">'+s.name+'</text>';
    svg+='<text x="'+X(s.rel_pct===null?0:s.rel_pct).toFixed(1)+'" y="'+(Y(s.change_pct)+12).toFixed(1)+'" text-anchor="middle" font-size="11" fill="'+(bhanuDark()?'#fff':'#111')+'">'+(s.change_pct>=0?'+':'')+s.change_pct.toFixed(2)+'%</text>';
  });
  svg+='<text x="'+(W/2)+'" y="'+(H-6)+'" text-anchor="middle" font-size="11" fill="#888">x = sector vs NIFTY (%) — right = beating NIFTY</text>';
  svg+='<text x="10" y="14" font-size="11" fill="#888">y = sector % change — up = green</text>';
  svg+='</svg>';
  el.innerHTML=svg;
  var list=document.getElementById(id+'-list');
  if(list){
    var html='<table><tr><th>Sector</th><th>% change</th><th>vs NIFTY</th><th>Stocks</th><th></th></tr>';
    p.sectors.forEach(function(s){
      var w=Math.min(100,Math.abs(s.change_pct)*40);
      html+='<tr><td>'+s.name+'</td><td class="'+(s.up?'up':'down')+'">'+(s.change_pct>=0?'+':'')+s.change_pct.toFixed(2)+'%</td><td>'+(s.rel_pct===null?'—':((s.rel_pct>=0?'+':'')+s.rel_pct.toFixed(2)+'%'))+'</td><td>'+s.stocks+'</td><td style="width:35%"><div class="sector-bar'+(s.up?'':' down')+'" style="width:'+w+'%"></div></td></tr>';
    });
    list.innerHTML=html+'</table><p class="muted">Bubble size = today\\'s traded value (close × volume). NIFTY change: '+(p.nifty_change===null||p.nifty_change===undefined?'—':p.nifty_change.toFixed(2)+'%')+'. As of '+p.as_of+'.</p>';
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
    if(x1===null||y1===null||y2===null){b.el.style.display='none';return;}
    if(x2===null){x2=w;}
    b.el.style.display='block';
    b.el.style.left=x1+'px';b.el.style.width=Math.max(0,x2-x1)+'px';
    b.el.style.top=Math.min(y1,y2)+'px';b.el.style.height=Math.max(0,Math.abs(y2-y1))+'px';
  });
}
function bhanuCandleUpdate(id, p){
  var st=(window.bhanuCandles||{})[id];
  if(!st){throw new Error('no candle chart');}
  if(!p||p.error||!p.candles||!p.candles.length){throw new Error(p&&p.error?p.error:'no candle data');}
  st.series.setData(p.candles);
  if(p.vwap&&p.vwap.length){
    if(st.vwap){st.vwap.setData(p.vwap);}
    else{st.vwap=st.chart.addLineSeries({color:'#7e57c2',lineWidth:1,priceLineVisible:false,lastValueVisible:false});st.vwap.setData(p.vwap);}
  }
  (st.priceLines||[]).forEach(function(h){try{st.series.removePriceLine(h);}catch(e){}});
  st.priceLines=[];
  (p.lines||[]).forEach(function(ln){
    st.priceLines.push(st.series.createPriceLine({price:ln.price,color:ln.color,lineWidth:1,
      lineStyle:ln.style||2,axisLabelVisible:true,title:ln.label}));
  });
  st.series.setMarkers(p.markers||[]);
  st.boxes.forEach(function(b){if(b.el.parentNode){b.el.parentNode.removeChild(b.el);}});
  st.boxes=bhanuCandleBoxes(st.wrap,p.boxes||[]);
  bhanuCandleLayout(st);
}
function bhanuCandleRender(id, p, refreshUrl){
  var wrap=document.getElementById(id+'-wrap');var el=document.getElementById(id);
  if(!wrap||!el)return;
  if(!p||!p.candles||p.candles.length===0){bhanuUnavailable(el,p&&p.error?p.error:'no price data');return;}
  if(typeof LightweightCharts==='undefined'){
    bhanuSvgCandles(el,p.candles);
    if(refreshUrl){
      window.bhanuRefreshers.push(function(){
        return fetch(refreshUrl).then(function(r){
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
  var cs=ch.addCandlestickSeries({upColor:'#26a69a',downColor:'#ef5350',
    wickUpColor:'#26a69a',wickDownColor:'#ef5350',borderVisible:false});
  cs.setData(p.candles);
  var vw=null;
  if(p.vwap&&p.vwap.length){
    vw=ch.addLineSeries({color:'#7e57c2',lineWidth:1,priceLineVisible:false,lastValueVisible:false});
    vw.setData(p.vwap);
  }
  var st={chart:ch,series:cs,vwap:vw,priceLines:[],boxes:[],wrap:wrap,el:el};
  (p.lines||[]).forEach(function(ln){
    st.priceLines.push(cs.createPriceLine({price:ln.price,color:ln.color,lineWidth:1,
      lineStyle:ln.style||2,axisLabelVisible:true,title:ln.label}));
  });
  if(p.markers&&p.markers.length){cs.setMarkers(p.markers);}
  st.boxes=bhanuCandleBoxes(wrap,p.boxes||[]);
  function layout(){bhanuCandleLayout(st);}
  ch.timeScale().subscribeVisibleTimeRangeChange(layout);
  ch.timeScale().subscribeVisibleLogicalRangeChange(layout);
  window.addEventListener('resize',function(){ch.applyOptions({width:el.clientWidth||900});layout();});
  window.bhanuCandles[id]=st;
  if(refreshUrl){
    window.bhanuRefreshers.push(function(){
      return fetch(refreshUrl).then(function(r){
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
  fetch(url).then(function(r){
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
      if(q.price===null||q.price===undefined){return;}
      var slug=String(q.symbol).replace(/[^A-Za-z0-9]/g,'').toLowerCase();
      var pe=document.getElementById('q-'+slug+'-price');
      var ce=document.getElementById('q-'+slug+'-chg');
      var ve=document.getElementById('q-'+slug+'-prev');
      if(pe){pe.textContent=Number(q.price).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});}
      if(ce&&q.change_pct!==null&&q.change_pct!==undefined){
        ce.textContent=(q.change_pct>=0?'+':'')+Number(q.change_pct).toFixed(2)+'%';
        ce.className='index-change '+(q.change_pct>=0?'up':'down');
      }
      if(ve&&q.prev_close!==null&&q.prev_close!==undefined){ve.textContent='prev close '+Number(q.prev_close).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});}
    });
    if(typeof bhanuMarkLive==='function'){bhanuMarkLive();}
  });
}
function bhanuQuoteRefresh(){
  bhanuQuoteFetch().catch(function(e){
    ['nsei','nsebank','bsesn'].forEach(function(slug){
      var pe=document.getElementById('q-'+slug+'-price');
      if(pe){pe.textContent='data unavailable';}
    });
  });
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
    var color=b.label.indexOf('Target')===0?'#26a69a':(b.label.indexOf('Stop')===0?'#ef5350':'#2962ff');
    rows+='<tr><td>'+b.label+'</td><td style="width:55%"><div style="height:18px;border-radius:4px;background:'+color+';width:'+w.toFixed(1)+'%"></div></td><td>'+b.count.toLocaleString('en-IN')+' ('+b.pct.toFixed(1)+'%)</td></tr>';
  });
  var avg='';
  if(hist.avg_r!==null&&hist.avg_r!==undefined){avg='<p>Average outcome across all '+hist.n.toLocaleString('en-IN')+' replays: <b>'+(hist.avg_r>=0?'+':'')+hist.avg_r.toFixed(2)+'R</b> (the bar the average sits in is the typical replay).</p>';}
  return '<h3>Monte Carlo distribution — '+hist.n.toLocaleString('en-IN')+' replays</h3><p class="muted">Each replay ran the setup on resampled 5m bars from the last 60 days. This is how the 10,000 checks split up:</p><table>'+rows+'</table>'+avg;
}
function bhanuMCRender(mc){
  var hist=document.getElementById('mc-hist');
  if(hist&&mc.histogram){hist.innerHTML=bhanuMCHistHTML(mc.histogram);}
  function set(id,txt){var e=document.getElementById(id);if(e){e.textContent=txt;}}
  set('mc-title','Monte Carlo Time Machine — '+Number(mc.n).toLocaleString('en-IN')+' replays');
  set('mc-setup',mc.symbol+' '+mc.direction+' entry '+mc.entry+', stop '+mc.stop+', target '+mc.target+'.');
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
function bhanuOrbBreakdown(sym){
  var bd=document.getElementById('orb-breakdown');
  if(!bd||!sym){return;}
  bd.innerHTML='<p class="loading">Signal breakdown load ho raha hai…</p>';
  fetch('/api/orbbreakdown?symbol='+encodeURIComponent(sym)).then(function(r){
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
  fetch('/api/levelsbreakdown?symbol='+encodeURIComponent(sym)).then(function(r){
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
  }).catch(function(e){el.innerHTML='<p class="muted">Time Machine load nahi ho paya — page refresh karein.</p>';});
}
</script>"""


def _line_chart_block(chart_id: str, label: str, yahoo_symbol: str,
                      height: int = 260) -> str:
    """A line-chart card, loaded lazily: the page renders instantly
    with a "loading" state and ``bhanuLazyLine`` fetches the data from
    /api/intraday — no Yahoo fetch ever blocks a page render."""
    return (
        f"<div class='card'><h3>{_esc(label)} — intraday</h3>"
        f"<div id='{chart_id}' class='chart-box' style='height:{height}px'>"
        f"<p class='loading'>Chart load ho raha hai…</p></div>"
        f"<script>bhanuLazyLine('{chart_id}', '{yahoo_symbol}');</script></div>"
    )


def _candle_chart_block(chart_id: str, symbol: str) -> str:
    """A candlestick + overlays card, loaded lazily: the page renders
    instantly with a "loading" state and the page loader
    (``bhanuOrbLoad`` / ``bhanuLevelsLoad``) fetches the data from
    /api/orbchart or /api/levelschart — no Yahoo fetch ever blocks a
    page render. ``symbol`` is the chart symbol when known from the
    URL, else '' (the loader fills it in from the scan)."""
    heading = (
        f"{_esc(symbol)} — candlestick with overlays"
        if symbol else "Candlestick with overlays"
    )
    return (
        f"<div class='card'><h3>{heading}</h3>"
        f"<p class='muted'>Candles: 5m. Purple line = VWAP. Dashed lines = ORB range / PDH / PDL / "
        f"support / resistance. Green box = target zone, red box = stop zone, "
        f"pale boxes = order blocks / fair value gaps. Arrows = CHoCH / BOS breaks.</p>"
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
        color = "#26a69a" if b["label"].startswith("Target") else (
            "#ef5350" if b["label"].startswith("Stop") else "#2962ff"
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
        f"<table>{rows}</table>{avg_line}"
    )


def _index_cards_html() -> str:
    """Three index cards (NIFTY / BANK NIFTY / SENSEX) with big price
    + change, filled in lazily by ``bhanuQuoteFetch`` (/api/quotes) —
    the page renders instantly with a "loading" state; no Yahoo fetch
    ever blocks a page render. Cell ids: ``q-<slug>-price`` /
    ``q-<slug>-chg`` / ``q-<slug>-prev``."""
    cards = ""
    for _symbol, label, slug in (
        ("^NSEI", "NIFTY 50", "nsei"),
        ("^NSEBANK", "BANK NIFTY", "nsebank"),
        ("^BSESN", "SENSEX", "bsesn"),
    ):
        cards += (
            f"<div class='index-card'><div class='index-name'>{_esc(label)}</div>"
            f"<div class='index-price' id='q-{slug}-price'>loading…</div>"
            f"<div class='index-change' id='q-{slug}-chg'>—</div>"
            f"<div class='index-prev' id='q-{slug}-prev'>prev close —</div></div>"
        )
    return f"<div class='index-cards'>{cards}</div>"


def _news_feed_html(lang: str) -> str:
    """Prominent news feed section (bot checked, automatic).

    Each headline is a card row with source + time and a "Bot ne check
    kiya" label. Used on home and /skills. A failed fetch shows an
    honest "unavailable" — never an empty or fake feed.
    """
    t = lambda k: i18n_mod.t(k, lang)
    try:
        news = _cached("news:NIFTY", 300, lambda: news_mod.check_symbol("NIFTY"))
    except Exception as exc:  # noqa: BLE001
        return (
            f"<div class='card'><h3>{t('news')} — News Feed</h3>"
            f"<p class='muted'>Bot ne abhi check kiya — News unavailable ({_esc(exc)})</p></div>"
        )
    latest = news.get("latest", [])[:8]
    sources = news.get("sources", {}) or {}
    source_line = (
        f"Bot ne abhi check kiya — NSE announcements: "
        f"{sources.get('nse_announcements', 'unavailable')}; "
        f"Yahoo headlines: {sources.get('yahoo_headlines', 'unavailable')}."
    )
    if not latest:
        return (
            f"<div class='card'><h3>{t('news')} — News Feed</h3>"
            f"<p class='muted'>{_esc(source_line)}</p>"
            f"<p>News unavailable right now — no headlines returned.</p></div>"
        )
    items = "".join(
        f"<div class='news-item'><b>{_esc(it.get('title', ''))}</b><br>"
        f"<span class='muted'>{_esc(it.get('source', ''))}"
        + (f" · {_esc(it.get('published', ''))}" if it.get("published") else "")
        + (f" · {', '.join(it.get('tags', []))}" if it.get("tags") else "")
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
        f"<div class='card'><h3>{t('news')} — News Feed</h3>"
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
            f"<tr><td>{_esc(h.get('symbol', ''))}</td><td>{_esc(h.get('direction', ''))}</td>"
            f"<td>{_esc(h.get('close', ''))}</td><td>{_esc(h.get('bar_time', ''))}</td>"
            f"<td>{_esc(h.get('range_high', ''))} – {_esc(h.get('range_low', ''))}</td>"
            f"<td>{tps}</td></tr>"
        )

    if not hits:
        return (
            f"<div class='card'><p>No ORB hits yet today (fetched {result.get('fetched')}, "
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
        f"<table><tr><th>Symbol</th><th>Direction</th><th>Close</th><th>Bar</th>"
        f"<th>Range high – low</th><th>TP1 / TP2 / TP3</th></tr>{rows}</table>{failed_html}{more_html}</div>"
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
    summary). Index quotes, intraday charts, flow, sector map, news
    and ORB hits all load lazily from /api/* after the page — no
    Yahoo fetch ever blocks a page render. Each section shows a
    "loading" state until its data arrives; a failed fetch shows an
    honest message, never a blank box."""
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

    # (a) Index intraday charts — lazy, one card per index.
    chart_cards = ""
    for yahoo_symbol, label in (("^NSEI", "NIFTY 50"), ("^NSEBANK", "BANK NIFTY"), ("^BSESN", "SENSEX")):
        cid = "chart-" + label.split()[0].lower().replace(" ", "")
        chart_cards += _line_chart_block(cid, label, yahoo_symbol)

    # (b) Smart Money Flow — all stocks' intraday % change on one chart.
    flow_html = (
        "<div class='card'><h3>Smart Money Flow — all stocks, one chart</h3>"
        "<p class='muted'>Every line is one stock's intraday % change from the open. "
        "Green = up, red = down, blue = NIFTY. The chart loads after the page "
        "(it fetches all 39 symbols) — if it fails, it says so honestly.</p>"
        "<div id='flow-chart' class='chart-box' style='height:340px'></div>"
        "<p class='muted' id='flow-chart-legend'></p></div>"
    )
    # (c) Sector Map bubble chart (lazy, same data as the flow).
    sector_html = (
        "<div class='card'><h3>Sector Map</h3>"
        "<p class='muted'>Each bubble is a sector: green = up, red = down, "
        "size = today's traded value. <a href='/sectors'>Full sector page</a>.</p>"
        "<div id='sector-map'></div><div id='sector-map-list'></div></div>"
    )
    # (d) News feed (prominent section, bot-checked) — lazy.
    news_html = (
        f"<div id='news-feed'><div class='card'><h3>{t('news')} — News Feed</h3>"
        f"<p class='loading'>News load ho raha hai…</p></div></div>"
    )
    # (e) ORB hits — lazy (top 10).
    orb_html = (
        "<div class='card' id='orb-hits'><h3>ORB Hits — today</h3>"
        "<p class='loading'>ORB scan load ho raha hai…</p></div>"
    )
    body = (
        f"<div class='card'><h3>{t('home')} — indices</h3>"
        f"{_index_cards_html()}"
        f"{paper_html}"
        f"<p>{t('flat_by')}: 15:15 IST. ORB range 09:15–09:30 IST.</p></div>"
        f"{chart_cards}{flow_html}{sector_html}{news_html}{orb_html}"
    )
    return _page(
        lang, t("home"), body,
        head_extra=_chart_lib_tag(),
        scripts=_chart_js()
        + f"<script>bhanuFlowChart('flow-chart');bhanuSectorMap('sector-map');"
          f"bhanuQuoteRefresh();bhanuNewsRefresh('{lang}');bhanuOrbHitsFetch();</script>",
        live=True,
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


def _signal_breakdown_html(sig: dict) -> str:
    """Signal breakdown card: decision + entry/SL/TP1/TP2/TP3 and every
    factor's verdict with its reason — so the news, Time Machine and
    volume contribution to the entry/exit is visible, not hidden."""
    if not sig or sig.get("decision") == "NO SIGNAL":
        return (
            "<div class='card'><h3>Signal breakdown</h3>"
            "<p>No ORB signal — no entry to judge.</p></div>"
        )

    def _f(value) -> str:
        return f"{value:,.2f}" if value is not None else "—"

    lines = ""
    for key, label in (("orb", "ORB"), ("levels", "Levels"),
                       ("news", "News"), ("mc", "Time Machine"),
                       ("volume", "Volume")):
        factor = (sig.get("factors") or {}).get(key)
        if not factor:
            continue
        lines += (
            f"<p><b>{label}: {_esc(factor.get('verdict', ''))}</b> — "
            f"{_esc(factor.get('reason', ''))}</p>"
        )
    size_note = ""
    if sig.get("size_factor") == 0.5:
        size_note = " (half size)"
    elif sig.get("size_factor") == 0.0:
        size_note = " (no trade)"
    return (
        f"<div class='card'><h3>Signal breakdown — "
        f"{_esc(sig.get('symbol', ''))} {_esc(sig.get('direction', ''))}</h3>"
        f"<p><b>Decision: {_esc(sig.get('decision', ''))}{size_note}</b> — "
        f"{_esc(sig.get('summary', ''))}</p>"
        f"<p>Entry {_f(sig.get('entry'))} | SL {_f(sig.get('stop'))} | "
        f"TP1 {_f(sig.get('tp1'))} | TP2 {_f(sig.get('tp2'))} | "
        f"TP3 {_f(sig.get('tp3'))}</p>"
        f"{lines}</div>"
    )


def _orb_scan() -> dict:
    """ORB scan result — fresh watchlist file (<300s) or a cached scan.
    Shared by /orb and /api/orbchart so both see the same hits."""
    watchlist = ROOT / "data" / "watchlist.json"
    if watchlist.exists() and time.time() - watchlist.stat().st_mtime < 300:
        try:
            return json.loads(watchlist.read_text())
        except Exception:  # noqa: BLE001 - fall through to a fresh scan
            pass
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
            f"<td>{_esc(r.get('signal') or 'None')}</td></tr>"
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
        f"<table><tr><th>Symbol</th><th>Price</th><th>Nearest Support</th>"
        f"<th>Nearest Resistance</th><th>PDH</th><th>PDL</th><th>ORB Entry</th>"
        f"<th>SL</th><th>TP1</th><th>TP2</th><th>TP3</th><th>Signal</th></tr>{body_rows}</table>"
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
    )


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
    )


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
        f"<div class='card'><p>{len(rows)} skills — what each does, where it shows, and its live status right now.</p>"
        f"<table><tr><th>DayTrader skill</th><th>Our module</th><th>What it does</th>"
        f"<th>Shows on</th><th>Live status</th></tr>{body_rows}</table>{note}</div>"
        f"{_news_feed_html(lang)}"
    )
    return _page(lang, t("skills"), body)


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
            f"<p>{_esc(setup.get('symbol', ''))} {_esc(setup.get('signal', ''))} — "
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
            f"<p id='mc-setup'>{_esc(mc.get('symbol', ''))} {_esc(mc.get('direction', ''))} "
            f"entry {_fmt(mc.get('entry'))}, stop {_fmt(mc.get('stop'))}, "
            f"target {_fmt(mc.get('target'))}.</p>"
            f"<table><tr><th>Outcome</th><th>Share</th></tr>"
            f"<tr><td>Target first (win)</td><td id='mc-target-pct'>{mc['target_first_pct']:.1f}%</td></tr>"
            f"<tr><td>Stop first (loss)</td><td id='mc-stop-pct'>{mc['stop_first_pct']:.1f}%</td></tr>"
            f"<tr><td>Neither (flat by 15:15)</td><td id='mc-neither-pct'>{mc['neither_pct']:.1f}%</td></tr></table>"
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
            f"<div class='card'><h3>Trade check — {tc['symbol']} {tc['direction']}</h3>"
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
            f"<div class='card'><h3>ORB backtest — NIFTY 5m ({_esc(bt.get('first_day', '?'))} to "
            f"{_esc(bt.get('last_day', '?'))}, {_esc(bt.get('sessions', '?'))} full sessions)</h3>"
            f"<table><tr><th>Direction</th><th>Fires</th><th>Target wins</th>"
            f"<th>Stop losses</th><th>Neither</th><th>Avg R</th></tr>{bt_rows}</table>"
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


def timemachine_page(lang: str) -> str:
    """Time Machine page — renders INSTANTLY; the setup / Monte Carlo
    / trade check / backtest body loads lazily from /api/timemachine
    (the 10,000-replay run is slow on a cold cache)."""
    t = lambda k: i18n_mod.t(k, lang)
    body = (
        "<div id='timemachine-body'>"
        "<p class='loading'>Time Machine load ho raha hai…</p></div>"
    )
    return _page(
        lang, t("timemachine"), body,
        scripts=_chart_js() + "<script>bhanuTimemachineLoad();</script>", live=True,
    )


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
    # Exit check — fresh news against an open position (cached 5 min
    # per symbol). A severe headline says EXIT NOW, a mild one says
    # EXIT EARLY; no news against the position says HOLD.
    exit_rows = ""
    for p in positions:
        try:
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
                f"<td>{_esc(p.get('direction', ''))}</td>"
                f"<td><b>{_esc(check['action'])}</b></td>"
                f"<td>{_esc(check['reason'])}</td></tr>"
            )
        except Exception as exc:  # noqa: BLE001 - a failed check never blocks the page
            exit_rows += (
                f"<tr><td>{_esc(p.get('id', ''))}</td>"
                f"<td>{_esc(p.get('symbol', ''))}</td>"
                f"<td>{_esc(p.get('direction', ''))}</td>"
                f"<td><b>HOLD</b></td>"
                f"<td>Exit check unavailable — {_esc(exc)}</td></tr>"
            )
    exit_html = (
        "<h3>Exit check — fresh news on open positions</h3>"
        "<table><tr><th>ID</th><th>Symbol</th><th>Dir</th>"
        "<th>Action</th><th>Why</th></tr>"
        f"{exit_rows or '<tr><td colspan=5>no open positions</td></tr>'}</table>"
    )
    body = (
        f"<p>Today: {summary.get('trades', 0)} trade(s), realised "
        f"₹{summary.get('realised_pnl', 0):,.2f}, risk taken "
        f"₹{summary.get('risk_taken', 0):,.2f}, flat: {summary.get('flat')}</p>"
        f"<h3>Open positions</h3><table><tr><th>ID</th><th>Symbol</th><th>Dir</th>"
        f"<th>Qty</th><th>Entry</th><th>Stop</th><th>Target</th></tr>{pos_rows or '<tr><td colspan=7>none</td></tr>'}</table>"
        f"{exit_html}"
        f"<h3>Closed trades</h3><table><tr><th>ID</th><th>Symbol</th><th>Dir</th>"
        f"<th>Exit</th><th>P&amp;L</th><th>Reason</th></tr>{tr_rows or '<tr><td colspan=6>none</td></tr>'}</table>"
    )
    return _page(lang, t("paper_trading"), body)


def _api_flow() -> dict:
    """Smart Money Flow JSON (cached 5 min — 39 symbols take a while)."""
    try:
        return _cached("api:flow", 300, charts_mod.flow_series)
    except Exception as exc:  # noqa: BLE001
        return {"series": [], "failed": [], "error": str(exc)}


def _api_sectors() -> dict:
    """Sector map JSON (same flow data, so no second round of fetches)."""
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
    """Live index quotes for the home-page refresh (cached 60s)."""
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
    """Intraday line/candle payload for one index (cached 60s)."""
    symbol = (query.get("symbol") or ["^NSEI"])[0]
    if symbol not in ("^NSEI", "^NSEBANK", "^BSESN"):
        return {"symbol": symbol, "line": [], "candles": [], "error": f"unknown symbol {symbol}"}
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
    """Monte Carlo + trade check + backtest (cached 10 min — the
    backtest refetches a month of bars, so it is slow). Returns the
    data plus a pre-rendered HTML body for the lazy page load."""
    try:
        data = _cached("timemachine", 600, timecheck_mod.collect_timemachine)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    out = dict(data)
    out["html"] = _timemachine_body(data)
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
    """Levels table HTML fragment (cached 60s). ``top_symbol`` is the
    first row, for the lazy candle chart."""
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
    """News feed HTML fragment for the home-page refresh."""
    try:
        return {"html": _news_feed_html(lang)}
    except Exception as exc:  # noqa: BLE001
        return {"html": "", "error": str(exc)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the console quiet
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        lang = _lang(query)
        try:
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
            if parsed.path == "/api/orbchart":
                self._send_json(_api_orbchart(query))
                return
            if parsed.path == "/api/levelschart":
                self._send_json(_api_levelschart(query))
                return
            if parsed.path == "/api/timemachine":
                self._send_json(_api_timemachine())
                return
            if parsed.path == "/api/orb":
                self._send_json(_api_orb(query, lang))
                return
            if parsed.path == "/api/orbbreakdown":
                self._send_json(_api_orbbreakdown(query, lang))
                return
            if parsed.path == "/api/levels":
                self._send_json(_api_levels(query, lang))
                return
            if parsed.path == "/api/levelsbreakdown":
                self._send_json(_api_levelsbreakdown(query, lang))
                return
            if parsed.path == "/api/news":
                self._send_json(_api_news(query, lang))
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
        except Exception as exc:  # noqa: BLE001 - never crash the server on a page
            self._send(_page(lang, "Error", f"<p>Something went wrong: {_esc(exc)}</p>"), 500)

    def _send_json(self, payload: dict):
        try:
            data = json.dumps(payload, default=str).encode("utf-8")
        except Exception as exc:  # noqa: BLE001
            data = json.dumps({"error": str(exc)}).encode("utf-8")
        self.send_response(200)
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
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Bhanu Indian Day Trader running at http://localhost:{PORT} (paper trading only)")
    server.serve_forever()


if __name__ == "__main__":
    main()
