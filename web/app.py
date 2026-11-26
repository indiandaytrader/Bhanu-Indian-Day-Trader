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

Charts render with TradingView Lightweight Charts from a CDN; data is
embedded as JSON (or fetched from /api/*). When a source is unavailable
the page says so plainly — no fake data is ever shown.
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


#: TradingView Lightweight Charts (pinned version, CDN). The pages check
#: for `window.LightweightCharts` and show "chart unavailable" when the
#: library could not load — a chart is never faked.
CHART_CDN = (
    "https://unpkg.com/lightweight-charts@4.2.0"
    "/dist/lightweight-charts.standalone.production.js"
)


def _page(lang: str, title: str, body: str, head_extra: str = "", scripts: str = "") -> str:
    t = lambda k: i18n_mod.t(k, lang)
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
</style>{head_extra}</head>
<body><button class="theme-btn" onclick="bhanuTheme()">🌙 / ☀️</button>
<h1>Bhanu Indian Day Trader</h1><nav>{nav}</nav>
<h2>{_esc(title)}</h2>{body}
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


def _chart_lib_tag() -> str:
    return f'<script src="{CHART_CDN}"></script>'


def _chart_js() -> str:
    """Shared chart helpers: line chart, candlestick + overlays, flow, sectors.

    Every renderer checks for the CDN library first and shows an honest
    "chart unavailable" message when it (or the data) is missing.
    """
    return """<script>
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
function bhanuLineChart(id){
  var el=document.getElementById(id);if(!el)return;
  var raw=document.getElementById(id+'-data');if(!raw){bhanuUnavailable(el,'no chart data');return;}
  var p;try{p=JSON.parse(raw.textContent);}catch(e){bhanuUnavailable(el,'bad chart data');return;}
  if(!p||!p.line||p.line.length===0){bhanuUnavailable(el,p&&p.error?p.error:'no price data');return;}
  if(typeof LightweightCharts==='undefined'){bhanuUnavailable(el,'chart library could not load (check internet)');return;}
  var o=bhanuChartOpts(260);o.width=el.clientWidth||900;
  var ch=LightweightCharts.createChart(el,o);
  var s=ch.addLineSeries({color:p.color||'#2962ff',lineWidth:2});
  s.setData(p.line);ch.timeScale().fitContent();
  window.addEventListener('resize',function(){ch.applyOptions({width:el.clientWidth||900});});
}
function bhanuFlowChart(id){
  var el=document.getElementById(id);if(!el)return;
  el.innerHTML='<p class="muted">Loading flow chart — all stocks…</p>';
  fetch('/api/flow').then(function(r){return r.json();}).then(function(p){
    if(!p||!p.series||p.series.length===0){bhanuUnavailable(el,p&&p.error?p.error:'no flow data');return;}
    if(typeof LightweightCharts==='undefined'){bhanuUnavailable(el,'chart library could not load (check internet)');return;}
    el.innerHTML='';
    var o=bhanuChartOpts(340);o.width=el.clientWidth||900;
    var ch=LightweightCharts.createChart(el,o);
    var up=0,down=0;
    p.series.forEach(function(s){
      if(s.up){up++;}else{down++;}
      var col=s.is_index?'#2962ff':(s.up?'#26a69a':'#ef5350');
      var ls=ch.addLineSeries({color:col,lineWidth:s.is_index?3:1,
        priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});
      ls.setData(s.line);
    });
    ch.timeScale().fitContent();
    var leg=document.getElementById(id+'-legend');
    if(leg){leg.textContent=up+' up / '+down+' down — each line is one stock\\'s intraday % change (NIFTY = blue). Failed: '+(p.failed?p.failed.length:0)+'. As of '+p.as_of+'.';}
    window.addEventListener('resize',function(){ch.applyOptions({width:el.clientWidth||900});});
  }).catch(function(e){bhanuUnavailable(el,'could not load flow data');});
}
function bhanuSectorMap(id){
  var el=document.getElementById(id);if(!el)return;
  el.innerHTML='<p class="muted">Loading sector map…</p>';
  fetch('/api/sectors').then(function(r){return r.json();}).then(function(p){
    if(!p||!p.sectors||p.sectors.length===0){bhanuUnavailable(el,p&&p.error?p.error:'no sector data');return;}
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
  }).catch(function(e){bhanuUnavailable(el,'could not load sector data');});
}
function bhanuCandleChart(id){
  var wrap=document.getElementById(id+'-wrap');var el=document.getElementById(id);
  if(!wrap||!el)return;
  var raw=document.getElementById(id+'-data');if(!raw){bhanuUnavailable(el,'no chart data');return;}
  var p;try{p=JSON.parse(raw.textContent);}catch(e){bhanuUnavailable(el,'bad chart data');return;}
  if(!p||!p.candles||p.candles.length===0){bhanuUnavailable(el,p&&p.error?p.error:'no price data');return;}
  if(typeof LightweightCharts==='undefined'){bhanuUnavailable(el,'chart library could not load (check internet)');return;}
  var o=bhanuChartOpts(420);o.width=el.clientWidth||900;
  var ch=LightweightCharts.createChart(el,o);
  var cs=ch.addCandlestickSeries({upColor:'#26a69a',downColor:'#ef5350',
    wickUpColor:'#26a69a',wickDownColor:'#ef5350',borderVisible:false});
  cs.setData(p.candles);
  if(p.vwap&&p.vwap.length){
    var vw=ch.addLineSeries({color:'#7e57c2',lineWidth:1,priceLineVisible:false,lastValueVisible:false});
    vw.setData(p.vwap);
  }
  (p.lines||[]).forEach(function(ln){
    cs.createPriceLine({price:ln.price,color:ln.color,lineWidth:1,
      lineStyle:ln.style||2,axisLabelVisible:true,title:ln.label});
  });
  if(p.markers&&p.markers.length){cs.setMarkers(p.markers);}
  var boxes=[];
  (p.boxes||[]).forEach(function(b){
    var d=document.createElement('div');
    d.style.cssText='position:absolute;pointer-events:none;border:1px dashed '+b.border+
      ';background:'+b.color+';font-size:10px;color:'+b.border+';padding:1px 4px;overflow:hidden;white-space:nowrap;';
    d.textContent=b.label;d.style.display='none';
    wrap.appendChild(d);
    boxes.push({spec:b,el:d});
  });
  function layout(){
    var w=el.clientWidth||900;
    boxes.forEach(function(b){
      var s=b.spec;
      var x1=ch.timeScale().timeToCoordinate(s.from);
      var x2=s.to?ch.timeScale().timeToCoordinate(s.to):w;
      var y1=cs.priceToCoordinate(s.top),y2=cs.priceToCoordinate(s.bottom);
      if(x1===null||y1===null||y2===null){b.el.style.display='none';return;}
      if(x2===null){x2=w;}
      b.el.style.display='block';
      b.el.style.left=x1+'px';b.el.style.width=Math.max(0,x2-x1)+'px';
      b.el.style.top=Math.min(y1,y2)+'px';b.el.style.height=Math.max(0,Math.abs(y2-y1))+'px';
    });
  }
  ch.timeScale().subscribeVisibleTimeRangeChange(layout);
  ch.timeScale().subscribeVisibleLogicalRangeChange(layout);
  window.addEventListener('resize',function(){ch.applyOptions({width:el.clientWidth||900});layout();});
  ch.timeScale().fitContent();layout();
}
</script>"""


def _line_chart_block(chart_id: str, payload: dict, height: int = 260) -> str:
    """A line-chart card: container + embedded JSON + init call."""
    if payload.get("error") or not payload.get("line"):
        return (
            f"<div class='card'><h3>{_esc(payload.get('symbol', ''))} — intraday</h3>"
            f"<p class='muted'>Chart unavailable — "
            f"{_esc(payload.get('error') or 'no price data')}.</p></div>"
        )
    return (
        f"<div class='card'><h3>{_esc(payload.get('symbol', ''))} — intraday</h3>"
        f"<div id='{chart_id}' class='chart-box' style='height:{height}px'></div>"
        f"<script type='application/json' id='{chart_id}-data'>{_chart_json(payload)}</script>"
        f"<script>bhanuLineChart('{chart_id}');</script></div>"
    )


def _candle_chart_block(chart_id: str, payload: dict) -> str:
    """A candlestick + overlays card: container + embedded JSON + init call."""
    if payload.get("error") and not payload.get("candles"):
        return (
            f"<div class='card'><h3>{_esc(payload.get('symbol', ''))} — candlestick</h3>"
            f"<p class='muted'>Chart unavailable — {_esc(payload.get('error'))}.</p></div>"
        )
    note = ""
    if payload.get("error"):
        note = f"<p class='muted'>Note: {_esc(payload['error'])} — levels shown where available.</p>"
    return (
        f"<div class='card'><h3>{_esc(payload.get('symbol', ''))} — candlestick with overlays</h3>"
        f"<p class='muted'>Candles: 5m. Purple line = VWAP. Dashed lines = ORB range / PDH / PDL / "
        f"support / resistance. Green box = target zone, red box = stop zone, "
        f"pale boxes = order blocks / fair value gaps. Arrows = CHoCH / BOS breaks.</p>"
        f"<div id='{chart_id}-wrap' class='chart-wrap'>"
        f"<div id='{chart_id}' class='chart-box tall'></div></div>{note}"
        f"<script type='application/json' id='{chart_id}-data'>{_chart_json(payload)}</script>"
        f"<script>bhanuCandleChart('{chart_id}');</script></div>"
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


def _orb_hits_html(lang: str, limit: int = 10) -> str:
    """Top ORB hits table (from the cached scan / watchlist file)."""
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
            return f"<p>ORB scan unavailable: {_esc(exc)}</p>"
    hits = result.get("hits", [])[:limit]
    if not hits:
        return (
            f"<p>No ORB hits yet today (fetched {result.get('fetched')}, "
            f"usable {result.get('usable')}, as of {_esc(result.get('as_of', ''))}).</p>"
        )
    rows = "".join(
        f"<tr><td>{_esc(h.get('symbol', ''))}</td><td>{_esc(h.get('direction', ''))}</td>"
        f"<td>{_esc(h.get('close', ''))}</td><td>{_esc(h.get('bar_time', ''))}</td>"
        f"<td>{_esc(h.get('range_high', ''))} – {_esc(h.get('range_low', ''))}</td></tr>"
        for h in hits
    )
    return (
        f"<p class='muted'>Top {len(hits)} of {len(result.get('hits', []))} ORB hits "
        f"(as of {_esc(result.get('as_of', ''))}) — "
        f"<a href='/orb?lang={lang}'>full scan</a>.</p>"
        f"<table><tr><th>Symbol</th><th>Direction</th><th>Close</th><th>Bar</th>"
        f"<th>Range high – low</th></tr>{rows}</table>"
    )


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

    # (a) Index intraday charts (one per index, today's 5m closes).
    chart_cards = ""
    for yahoo_symbol, label in (("^NSEI", "NIFTY 50"), ("^NSEBANK", "BANK NIFTY"), ("^BSESN", "SENSEX")):
        cid = "chart-" + label.split()[0].lower().replace(" ", "")
        try:
            payload = _cached(
                f"chart:{yahoo_symbol}", 60,
                lambda ys=yahoo_symbol: charts_mod.intraday_series(ys),
            )
            payload = dict(payload)
            payload["symbol"] = label
            payload["color"] = charts_mod.COLOR_INDEX
        except Exception as exc:  # noqa: BLE001
            payload = {"symbol": label, "line": [], "error": str(exc)}
        chart_cards += _line_chart_block(cid, payload)

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
    # (d) News feed (prominent section, bot-checked).
    news_html = _news_feed_html(lang)
    # (e) ORB hits table.
    orb_html = (
        "<div class='card'><h3>ORB Hits — today</h3>"
        + _orb_hits_html(lang)
        + "</div>"
    )
    body = (
        f"<div class='card'><table><tr><th>Index</th><th>Price</th><th>Change</th><th></th></tr>{rows}</table>"
        f"{paper_html}"
        f"<p>{t('flat_by')}: 15:15 IST. ORB range 09:15–09:30 IST.</p></div>"
        f"{chart_cards}{flow_html}{sector_html}{news_html}{orb_html}"
    )
    return _page(
        lang, t("home"), body,
        head_extra=_chart_lib_tag(),
        scripts=_chart_js()
        + "<script>bhanuFlowChart('flow-chart');bhanuSectorMap('sector-map');</script>",
    )


def orb_page(lang: str, query: dict | None = None) -> str:
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

    # Candlestick chart for the selected hit (default: top hit).
    chart_html = ""
    if hits:
        wanted = ((query or {}).get("symbol") or [""])[0].upper()
        picked = next((h for h in hits if h.get("symbol", "").upper() == wanted), hits[0])
        try:
            payload = _cached(
                f"orbchart:{picked['yahoo_symbol']}", 60,
                lambda: charts_mod.orb_chart(picked["yahoo_symbol"], picked["symbol"]),
            )
        except Exception as exc:  # noqa: BLE001
            payload = {"symbol": picked["symbol"], "candles": [], "error": str(exc)}
        chart_html = _candle_chart_block("orb-chart", payload)
        switcher = " | ".join(
            f"<a href='/orb?symbol={_esc(h.get('symbol', ''))}&lang={lang}'>"
            f"{_esc(h.get('symbol', ''))}</a>"
            for h in hits[:12]
        )
        chart_html = f"<p class='muted'>Chart symbol: {switcher}</p>" + chart_html
    else:
        chart_html = (
            "<div class='card'><h3>Candlestick</h3>"
            "<p class='muted'>No ORB trigger yet today — no chart to show. "
            "The first 5m bar closing outside the 09:15–09:30 range creates one.</p></div>"
        )

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
        f"{chart_html}"
        f"<div class='card'><p>Fetched {result.get('fetched')} symbols, usable {result.get('usable')}, "
        f"hits {len(hits)} (as of {_esc(result.get('as_of', ''))}). "
        f"A hit is the first completed 5m bar that CLOSES above/below the 09:15–09:30 range — "
        f"wicks do not count, and a hit is stale once the price has moved on.</p>"
        f"<table><tr><th>Symbol</th><th>Direction</th><th>Close</th><th>Bar</th>"
        f"<th>Range high – low</th></tr>{rows}</table>{failed_html}</div>"
    )
    return _page(
        lang, t("orb_scanner"), body,
        head_extra=_chart_lib_tag(), scripts=_chart_js(),
    )


def levels_page(lang: str, query: dict | None = None) -> str:
    t = lambda k: i18n_mod.t(k, lang)
    try:
        result = _cached("levels", 60, levels_mod.scan_levels)
    except Exception as exc:  # noqa: BLE001
        return _page(lang, t("levels"), f"<p>levels unavailable: {_esc(exc)}</p>")
    rows = result.get("levels", [])
    failed = result.get("failed", [])

    # Candlestick chart for the selected index (default NIFTY).
    chart_html = ""
    if rows:
        wanted = ((query or {}).get("symbol") or ["NIFTY"])[0].upper()
        picked = next((r for r in rows if r.get("symbol", "").upper() == wanted), rows[0])
        yahoo_symbol = data_mod.INDICES.get(picked["symbol"], "^NSEI")
        try:
            payload = _cached(
                f"levelschart:{picked['symbol']}", 60,
                lambda: charts_mod.levels_chart(yahoo_symbol, picked["symbol"]),
            )
        except Exception as exc:  # noqa: BLE001
            payload = {"symbol": picked["symbol"], "candles": [], "error": str(exc)}
        switcher = " | ".join(
            f"<a href='/levels?symbol={_esc(r.get('symbol', ''))}&lang={lang}'>"
            f"{_esc(r.get('symbol', ''))}</a>"
            for r in rows
        )
        chart_html = f"<p class='muted'>Chart symbol: {switcher}</p>" + _candle_chart_block("levels-chart", payload)
    else:
        chart_html = (
            "<div class='card'><h3>Candlestick</h3>"
            "<p class='muted'>Levels unavailable — no chart to show.</p></div>"
        )

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
        f"{chart_html}"
        f"<div class='card'><p>As of {_esc(result.get('run_at_ist', ''))}. One row per index — "
        f"enter here, exit here, support here, resistance here.</p>"
        f"<table><tr><th>Symbol</th><th>Price</th><th>Nearest Support</th>"
        f"<th>Nearest Resistance</th><th>PDH</th><th>PDL</th><th>ORB Entry</th>"
        f"<th>SL</th><th>TP</th><th>Signal</th></tr>{body_rows}</table>"
        f"{failed_html}{note}</div>"
    )
    return _page(
        lang, t("levels"), body,
        head_extra=_chart_lib_tag(), scripts=_chart_js(),
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
        "weight proxy, labelled as such. A sector whose members all failed to "
        "fetch is listed as failed, never shown as flat.</p></div>"
    )
    return _page(
        lang, "Sectors", body,
        scripts=_chart_js() + "<script>bhanuSectorMap('sector-map');</script>",
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
                f"<div class='card'>{_mc_histogram_svg(mc['histogram'])}</div>"
            )
        mc_html = (
            hist_html
            + f"<div class='card'><h3>Monte Carlo Time Machine — {mc['n']:,} replays</h3>"
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
