/* web/static/pro_terminal.js
 * Bhanu Indian Day Trader — Pro Terminal Frontend Engine
 * Pure vanilla JS (zero build step, zero external dependencies).
 * Works with web/static/terminal.css and the stdlib web/app.py backend.
 *
 * Key guarantees:
 *  - Default theme: Crisp White / Light (data-theme="light"), persisted dark toggle
 *  - 1-second IST session clock + 2-second live polling of real /api/* endpoints
 *  - Smooth 60fps canvas & chart easing when market is OPEN
 *  - HARD MARKET-CLOSED FREEZE: stops polling, freezes prices/charts flat,
 *    halts pulsing/radar animations, and displays a clear "Market Closed" badge
 *  - Exposed renderMcHistCanvas (window + BhanuPro) with mean line & SD zones
 *  - Real ORB & signal state rendering (SCANNED / BREAKOUT / TRADE READY,
 *    NEW / CONFIRMED / RETESTED / INVALIDATED) with CALL (green) / PUT (red)
 *  - Zero fake/fabricated market prices — uses only real backend responses
 */
(function (global) {
  'use strict';

  // =========================================================================
  // 1. STATE & CONFIGURATION
  // =========================================================================
  const IST_TZ = 'Asia/Kolkata';
  const STORAGE_KEYS = {
    theme: 'bhanu-theme',
    alerts: 'bhanu-pro-alerts-v1',
    journal: 'bhanu-pro-journal-v1',
    paper: 'bhanu-pro-paper-v1',
    killSwitch: 'bhanu-pro-kill-v1'
  };

  const state = {
    theme: 'light',
    marketOpen: false,
    marketStatusChecked: false,
    pollerStatus: null,
    lastQuote: null,
    quotes: {},          // symbol -> { price, prev_close, change_pct, timestamp, stale, error }
    liveTicks: {},       // symbol -> { ltp, prev, change_pct, time }
    orbSnapshot: null,   // latest real ORB scan payload from /api/orb
    liveAnim: {},        // key -> { token, start }
    canvasCharts: {},    // canvasId -> { canvas, type, candles, line, vwap, levels, overlays }
    timers: {
      clock: null,
      livePoll: null,
      marketCheck: null,
      botRibbon: null,
      replay: null,
      paperQuote: null
    },
    bot: {
      scanning: false,
      scanCount: 0,
      tickerIdx: 0,
      symbols: [],
      topSetups: [],
      bestSetup: null
    },
    paper: {
      capital: 500000,
      realized: 0,
      positions: [],
      nextId: 1
    },
    killSwitch: false,
    alerts: [],
    journal: [],
    replay: {
      candles: [],
      idx: 6,
      playing: false
    },
    inFlight: {
      quotes: false,
      live: false,
      poller: false
    }
  };

  // =========================================================================
  // 2. UTILITIES (FORMATTING, SAFE FETCH, TOAST, AUDIO PING)
  // =========================================================================
  function $(id) {
    return document.getElementById(id);
  }

  function escHtml(str) {
    return String(str == null ? '' : str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function fmtNum(val, dec) {
    if (val === null || val === undefined || isNaN(Number(val))) return '—';
    const d = typeof dec === 'number' ? dec : 2;
    return Number(val).toLocaleString('en-IN', {
      minimumFractionDigits: d,
      maximumFractionDigits: d
    });
  }

  function fmtPct(val) {
    if (val === null || val === undefined || isNaN(Number(val))) return '—';
    const n = Number(val);
    return (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
  }

  function fmtIstStamp(tsSeconds) {
    if (!tsSeconds) return '';
    try {
      const dt = new Date(Number(tsSeconds) * 1000);
      return dt.toLocaleString('en-IN', {
        timeZone: IST_TZ,
        day: '2-digit',
        month: 'short',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false
      }) + ' IST';
    } catch (e) {
      return '';
    }
  }

  function optLabel(dir) {
    const d = String(dir || '').toLowerCase();
    if (d === 'short' || d === 'put' || d === 'sell') return 'PUT';
    if (d === 'long' || d === 'call' || d === 'buy') return 'CALL';
    return String(dir || '').toUpperCase();
  }

  function optClass(dir) {
    return optLabel(dir) === 'PUT' ? 'opt-put' : 'opt-call';
  }

  function stateBadgeHtml(rawState) {
    const norm = String(rawState || 'scanned').trim().toLowerCase();
    const map = {
      'warming up': { cls: 'state-scanned', label: 'WARMING UP' },
      'scanned': { cls: 'state-scanned', label: 'SCANNED' },
      'breakout': { cls: 'state-breakout', label: 'BREAKOUT' },
      'trade ready': { cls: 'state-ready', label: 'TRADE READY' },
      'new': { cls: 'state-new', label: 'NEW' },
      'confirmed': { cls: 'state-confirmed', label: 'CONFIRMED' },
      'retested': { cls: 'state-retested', label: 'RETESTED' },
      'invalidated': { cls: 'state-invalidated', label: 'INVALIDATED' }
    };
    const info = map[norm] || { cls: 'state-scanned', label: norm.toUpperCase() };
    return '<span class="state-pill ' + info.cls + '">' + escHtml(info.label) + '</span>';
  }

  function fetchJson(url, timeoutMs) {
    const ms = timeoutMs || 15000;
    if (typeof AbortController === 'undefined') {
      return fetch(url, { cache: 'no-store' }).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      });
    }
    const ctrl = new AbortController();
    const timer = setTimeout(function () { ctrl.abort(); }, ms);
    return fetch(url, { signal: ctrl.signal, cache: 'no-store' })
      .then(function (r) {
        clearTimeout(timer);
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .catch(function (err) {
        clearTimeout(timer);
        if (err && err.name === 'AbortError') throw new Error('Request timed out');
        throw err;
      });
  }

  function fetchAsyncDetail(url) {
    const deadline = Date.now() + 90000;
    function poll() {
      return fetch(url, {
        headers: { Prefer: 'respond-async' },
        cache: 'no-store'
      }).then(function (res) {
        if (res.status === 202) {
          if (Date.now() > deadline) throw new Error('Detail feed timed out');
          return new Promise(function (resolve) { setTimeout(resolve, 1000); }).then(poll);
        }
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      });
    }
    return poll();
  }

  let toastTimer = null;
  function showToast(msg) {
    let el = $('pro-toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'pro-toast';
      el.className = 'pro-toast';
      el.setAttribute('role', 'status');
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      el.classList.remove('show');
    }, 2800);
  }

  function playAlertPing() {
    try {
      const AudioCtx = global.AudioContext || global.webkitAudioContext;
      if (!AudioCtx) return;
      const ctx = new AudioCtx();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(880, ctx.currentTime);
      osc.frequency.exponentialRampToValueAtTime(1320, ctx.currentTime + 0.14);
      gain.gain.setValueAtTime(0.08, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.25);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + 0.26);
    } catch (e) { /* audio blocked by browser policy */ }
  }

  function loadStorage(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch (e) {
      return fallback;
    }
  }

  function saveStorage(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch (e) { /* private mode or quota full */ }
  }

  // =========================================================================
  // 3. THEME ENGINE (WHITE/LIGHT DEFAULT + DARK TOGGLE)
  // =========================================================================
  function getCanvasPalette() {
    const dark = state.theme === 'dark';
    const forestGreen = '#2f9e5f';
    const forestGreenSoft = dark ? 'rgba(47, 158, 95, 0.18)' : 'rgba(47, 158, 95, 0.12)';
    const brickRed = '#d6455d';
    const brickRedSoft = dark ? 'rgba(214, 69, 93, 0.18)' : 'rgba(214, 69, 93, 0.12)';
    return {
      bg: dark ? '#0D131F' : '#FFFFFF',
      grid: dark ? 'rgba(148, 163, 184, 0.12)' : 'rgba(15, 23, 42, 0.07)',
      divider: dark ? 'rgba(148, 163, 184, 0.34)' : 'rgba(15, 23, 42, 0.22)',
      axisText: dark ? '#94A3B8' : '#475569',
      pillText: '#FFFFFF',
      bull: forestGreen,
      bullSoft: forestGreenSoft,
      bear: brickRed,
      bearSoft: brickRedSoft,
      blue: dark ? '#60A5FA' : '#2563EB',
      gold: dark ? '#F59E0B' : '#D97706',
      purple: dark ? '#A855F7' : '#7C3AED',
      obOpacity: 0.38,
      fvgOpacity: 0.30
    };
  }

  function applyTheme(theme) {
    const next = theme === 'dark' ? 'dark' : 'light';
    state.theme = next;
    document.documentElement.setAttribute('data-theme', next);
    try {
      localStorage.setItem(STORAGE_KEYS.theme, next);
    } catch (e) { /* ignore */ }

    const iconEl = $('pro-theme-icon');
    if (iconEl) iconEl.textContent = next === 'dark' ? '☀️' : '🌙';
    const labelEl = $('pro-theme-label');
    if (labelEl) labelEl.textContent = next === 'dark' ? 'Light' : 'Dark';

    // Update any mounted LightweightCharts instances
    if (typeof global.bhanuChartOpts === 'function') {
      const opts = global.bhanuChartOpts();
      const allCharts = []
        .concat(Object.values(global.bhanuLines || {}))
        .concat(Object.values(global.bhanuCandles || {}));
      allCharts.forEach(function (st) {
        if (st && st.chart) {
          try { st.chart.applyOptions({ layout: opts.layout, grid: opts.grid }); } catch (e) {}
        }
      });
    }

    // Re-paint all custom canvas charts & Monte Carlo histograms in the new theme
    Object.keys(state.canvasCharts).forEach(function (cid) {
      renderCanvasChart(cid);
    });
    renderAllMcCanvases();
    if ($('replay-canvas')) drawReplayCanvas();
  }

  function toggleTheme() {
    applyTheme(state.theme === 'dark' ? 'light' : 'dark');
  }

  function initTheme() {
    let saved = 'light';
    try {
      const s = localStorage.getItem(STORAGE_KEYS.theme);
      if (s === 'dark' || s === 'light') saved = s;
    } catch (e) { /* default light */ }
    applyTheme(saved);
  }

  // =========================================================================
  // 4. 1-SECOND IST LIVE CLOCK & MARKET-CLOSED FREEZE CONTROLLER
  // =========================================================================
  const clockFmt = new Intl.DateTimeFormat('en-GB', {
    timeZone: IST_TZ,
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false
  });

  function isNseHoursLocal() {
    try {
      const parts = new Intl.DateTimeFormat('en-US', {
        timeZone: IST_TZ,
        weekday: 'short',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false
      }).formatToParts(new Date());
      const map = {};
      parts.forEach(function (p) { map[p.type] = p.value; });
      if (map.weekday === 'Sat' || map.weekday === 'Sun') return false;
      const mins = parseInt(map.hour, 10) * 60 + parseInt(map.minute, 10);
      return mins >= (9 * 60 + 15) && mins < (15 * 60 + 30);
    } catch (e) {
      return false;
    }
  }

  function paintClock() {
    const nowStr = clockFmt.format(new Date()) + ' IST';
    const el = $('session-clock');
    if (el) el.textContent = nowStr;

    // Update live indicator clock ONLY when market is OPEN
    const ind = $('bhanu-live-indicator');
    if (ind && state.marketOpen && ind.getAttribute('data-last') && ind.getAttribute('data-failed') !== '1') {
      ind.innerHTML = '<span class="dot"></span> LIVE · ' + nowStr +
        ' · data ' + escHtml(ind.getAttribute('data-last'));
    }
  }

  function setMarketOpenState(isOpen, pollerInfo) {
    const changed = (!state.marketStatusChecked) || (state.marketOpen !== isOpen);
    state.marketOpen = Boolean(isOpen);
    state.marketStatusChecked = true;
    if (pollerInfo) state.pollerStatus = pollerInfo;

    document.body.classList.toggle('is-market-closed', !state.marketOpen);
    updateMarketBadge();

    const ind = $('bhanu-live-indicator');
    if (ind) {
      ind.classList.toggle('market-closed', !state.marketOpen);
      if (!state.marketOpen) {
        ind.innerHTML = '<span class="dot"></span> Market Closed · Prices &amp; charts frozen (last NSE snapshot)';
      } else if (!ind.getAttribute('data-last')) {
        ind.innerHTML = '<span class="dot"></span> LIVE · loading…';
      }
    }

    const moodBadge = $('mood-status-badge');
    if (moodBadge && !state.marketOpen) {
      moodBadge.textContent = 'MARKET CLOSED · FROZEN';
      moodBadge.className = 'pill down';
    }

    const botTicker = $('bot-live-ticker');
    const botStatus = $('bot-scan-status');
    if (!state.marketOpen) {
      if (botTicker) botTicker.textContent = 'Market Closed — Auto-scan paused (last session snapshot)';
      if (botStatus) botStatus.textContent = 'MARKET CLOSED';
      stopLiveTimers();
    } else if (changed) {
      if (botStatus) botStatus.textContent = 'LIVE · AUTO-SCAN';
      startLiveTimers();
    }
  }

  function checkPollerMarketStatus() {
    if (state.inFlight.poller) return Promise.resolve();
    state.inFlight.poller = true;
    return fetchJson('/api/poller', 8000)
      .then(function (st) {
        state.inFlight.poller = false;
        const open = (st && typeof st.market_open === 'boolean')
          ? st.market_open
          : isNseHoursLocal();
        setMarketOpenState(open, st);
        updateBotPollerMetrics(st);
      })
      .catch(function () {
        state.inFlight.poller = false;
        setMarketOpenState(isNseHoursLocal(), null);
      });
  }

  function stopLiveTimers() {
    if (state.timers.livePoll) {
      clearInterval(state.timers.livePoll);
      state.timers.livePoll = null;
    }
    if (state.timers.botRibbon) {
      clearInterval(state.timers.botRibbon);
      state.timers.botRibbon = null;
    }
    if (global.bhanuQuoteTimer) {
      clearInterval(global.bhanuQuoteTimer);
      global.bhanuQuoteTimer = null;
    }
    Object.keys(global.bhanuLiveTimers || {}).forEach(function (k) {
      clearInterval(global.bhanuLiveTimers[k]);
      delete global.bhanuLiveTimers[k];
    });
    state.liveAnim = {};
  }

  function updateMarketBadge() {
    const badge = $('market-status-badge');
    if (!badge) return;
    if (state.marketOpen) {
      badge.classList.remove('closed');
      badge.innerHTML = '<span class="dot"></span> LIVE · 2s';
    } else {
      badge.classList.add('closed');
      const lastQuote = state.lastQuote;
      if (lastQuote && lastQuote.price !== null && lastQuote.price !== undefined) {
        const stamp = fmtIstStamp(lastQuote.timestamp);
        badge.innerHTML = '<span class="dot"></span> Market Closed · ' +
          fmtNum(lastQuote.price, 2) +
          (stamp ? ' · ' + escHtml(stamp) : '');
      } else {
        badge.innerHTML = '<span class="dot"></span> Market Closed';
      }
    }
  }

  function startLiveTimers() {
    if (!state.marketOpen) return;
    if (!state.timers.livePoll) {
      state.timers.livePoll = setInterval(runTwoSecondLivePoll, 2000);
    }
    if (!state.timers.botRibbon) {
      state.timers.botRibbon = setInterval(tickBotAutoRibbon, 1200);
    }
  }

  // =========================================================================
  // 5. ORB & SIGNAL STATE RENDERING (R1-D1 — REAL /api/orb + LIVE TICKS)
  // =========================================================================
  function deriveLiveOrbState(item, livePrice) {
    const baseState = String(item.state || 'scanned').toLowerCase();
    const rh = Number(item.range_high);
    const rl = Number(item.range_low);
    const dir = String(item.direction || '').toLowerCase();
    let orbState = baseState;
    let sigState = item.signal_state ? String(item.signal_state).toLowerCase() : null;

    if (livePrice != null && !isNaN(livePrice) && !isNaN(rh) && !isNaN(rl) && rh > rl) {
      if (baseState === 'scanned') {
        if (livePrice > rh || livePrice < rl) {
          orbState = 'breakout';
          sigState = sigState || 'new';
        }
      } else if (baseState === 'breakout' || baseState === 'trade ready') {
        if (dir === 'long') {
          if (livePrice < rl) {
            sigState = 'invalidated';
          } else if (livePrice <= rh * 1.0015 && livePrice >= rl) {
            sigState = 'retested';
          } else if (!sigState) {
            sigState = baseState === 'trade ready' ? 'confirmed' : 'new';
          }
        } else if (dir === 'short') {
          if (livePrice > rh) {
            sigState = 'invalidated';
          } else if (livePrice >= rl * 0.9985 && livePrice <= rh) {
            sigState = 'retested';
          } else if (!sigState) {
            sigState = baseState === 'trade ready' ? 'confirmed' : 'new';
          }
        }
      }
    }

    if (!sigState) {
      if (orbState === 'trade ready') sigState = 'confirmed';
      else if (orbState === 'breakout') sigState = 'new';
      else sigState = 'new';
    }
    return { orbState: orbState, sigState: sigState };
  }

  function renderOrbState(orbData, targetId) {
    if (!orbData) return;
    state.orbSnapshot = orbData;

    const container = $(targetId || 'orb-hits') || $('orb-table');
    if (!container) return;

    const asOf = orbData.as_of || orbData.run_at_ist || '—';
    const scanState = String(orbData.state || 'scanned');
    const hits = Array.isArray(orbData.hits) ? orbData.hits : [];
    const scanned = Array.isArray(orbData.scanned) ? orbData.scanned : [];

    // Update any top ORB state summary badge if present
    const stateBadgeHost = $('orb-global-state');
    if (stateBadgeHost) {
      stateBadgeHost.innerHTML = stateBadgeHtml(scanState);
    }

    // If the server already rendered the full HTML table, only patch live state cells
    if (orbData.html && !orbData.forceClientRender) {
      container.innerHTML = orbData.html;
      syncOrbStatesWithLivePrices();
      return;
    }

    let html = '<h3>ORB Hits — today</h3>' +
      '<p class="muted">Scan status: ' + stateBadgeHtml(scanState) +
      ' · ' + hits.length + ' breakout(s) · ' + scanned.length + ' scanned · As of ' + escHtml(asOf) + '</p>';

    if (scanState.toLowerCase() === 'warming up' && !hits.length && !scanned.length) {
      html += '<p class="loading">ORB scan warming up (09:15–09:30 IST opening range)…</p>';
    } else if (!hits.length && !scanned.length) {
      html += '<p class="empty-state">No ORB signals detected yet today.</p>';
    } else {
      html += '<div class="table-wrap"><table style="min-width:640px">' +
        '<tr><th>Symbol</th><th>Direction</th><th>Close</th><th>Bar</th><th>Range</th><th>ORB State</th><th>Signal</th></tr>';

      hits.forEach(function (h) {
        const livePx = resolveLivePriceForSymbol(h.symbol);
        const derived = deriveLiveOrbState(h, livePx);
        const dirHtml = h.direction
          ? '<span class="' + optClass(h.direction) + '">' + optLabel(h.direction) + '</span>'
          : '—';
        html += '<tr data-orb-sym="' + escHtml(h.symbol) + '" data-orb-rh="' + escHtml(h.range_high) +
          '" data-orb-rl="' + escHtml(h.range_low) + '" data-orb-dir="' + escHtml(h.direction || '') +
          '" data-orb-state="' + escHtml(h.state || 'breakout') + '">' +
          '<td><a href="/stock?symbol=' + encodeURIComponent(h.symbol) + '"><b>' + escHtml(h.symbol) + '</b></a></td>' +
          '<td>' + dirHtml + '</td>' +
          '<td class="mono">' + fmtNum(h.close, 2) + '</td>' +
          '<td class="mono">' + escHtml(h.bar_time || '—') + '</td>' +
          '<td class="mono">' + fmtNum(h.range_high, 2) + ' – ' + fmtNum(h.range_low, 2) + '</td>' +
          '<td class="orb-state-cell">' + stateBadgeHtml(derived.orbState) + '</td>' +
          '<td class="sig-state-cell">' + stateBadgeHtml(derived.sigState) + '</td>' +
          '</tr>';
      });

      scanned.slice(0, 8).forEach(function (s) {
        const livePx = resolveLivePriceForSymbol(s.symbol);
        const derived = deriveLiveOrbState(s, livePx);
        html += '<tr data-orb-sym="' + escHtml(s.symbol) + '" data-orb-rh="' + escHtml(s.range_high) +
          '" data-orb-rl="' + escHtml(s.range_low) + '" data-orb-dir="" data-orb-state="scanned">' +
          '<td><a href="/stock?symbol=' + encodeURIComponent(s.symbol) + '"><b>' + escHtml(s.symbol) + '</b></a></td>' +
          '<td>—</td><td class="mono">—</td><td class="mono">—</td>' +
          '<td class="mono">' + fmtNum(s.range_high, 2) + ' – ' + fmtNum(s.range_low, 2) + '</td>' +
          '<td class="orb-state-cell">' + stateBadgeHtml(derived.orbState) + '</td>' +
          '<td class="sig-state-cell">' + stateBadgeHtml(derived.sigState) + '</td>' +
          '</tr>';
      });

      html += '</table></div>';
    }

    container.innerHTML = html;
  }

  function syncOrbStatesWithLivePrices() {
    const rows = document.querySelectorAll('tr[data-orb-sym]');
    if (!rows.length) return;
    rows.forEach(function (tr) {
      const sym = tr.getAttribute('data-orb-sym');
      const livePx = resolveLivePriceForSymbol(sym);
      if (livePx === null) return;
      const item = {
        state: tr.getAttribute('data-orb-state') || 'scanned',
        direction: tr.getAttribute('data-orb-dir') || '',
        range_high: parseFloat(tr.getAttribute('data-orb-rh')),
        range_low: parseFloat(tr.getAttribute('data-orb-rl')),
        signal_state: tr.getAttribute('data-sig-state') || ''
      };
      const derived = deriveLiveOrbState(item, livePx);
      const orbCell = tr.querySelector('.orb-state-cell');
      const sigCell = tr.querySelector('.sig-state-cell');
      if (orbCell) orbCell.innerHTML = stateBadgeHtml(derived.orbState);
      if (sigCell) sigCell.innerHTML = stateBadgeHtml(derived.sigState);
    });
  }

  // =========================================================================
  // 5b. SMOOTH PRICE EASING & 2-SECOND LIVE POLLING
  // =========================================================================
  function easeInOutQuad(t) {
    return t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
  }

  function glideValue(key, fromVal, toVal, onStep) {
    if (!state.marketOpen || fromVal === toVal || isNaN(fromVal) || isNaN(toVal)) {
      onStep(toVal);
      return;
    }
    const token = ((state.liveAnim[key] && state.liveAnim[key].token) || 0) + 1;
    state.liveAnim[key] = { token: token, start: null };
    const raf = global.requestAnimationFrame || function (fn) { return setTimeout(function () { fn(Date.now()); }, 16); };

    function frame(ts) {
      if (!state.marketOpen) return;
      const anim = state.liveAnim[key];
      if (!anim || anim.token !== token) return;
      if (anim.start === null) anim.start = ts;
      const k = Math.min(1, (ts - anim.start) / 1900);
      const v = fromVal + (toVal - fromVal) * easeInOutQuad(k);
      onStep(v);
      if (k < 1) raf(frame);
    }
    raf(frame);
  }

  function applyQuotesSnapshot(quotes) {
    if (!Array.isArray(quotes)) return;
    quotes.forEach(function (q) {
      const sym = q.symbol;
      const prevKnown = state.quotes[sym] ? state.quotes[sym].price : null;
      state.quotes[sym] = q;
      if (q.price !== null && q.price !== undefined) {
        state.lastQuote = q;
      }
      updateMarketBadge();

      const slug = String(sym).replace(/[^A-Za-z0-9]/g, '').toLowerCase();
      const priceEl = $('q-' + slug + '-price');
      const chgEl = $('q-' + slug + '-chg');
      const prevEl = $('q-' + slug + '-prev');
      const cardEl = $('q-' + slug + '-card');
      const statusEl = $('q-' + slug + '-status');
      const sourceEl = $('q-' + slug + '-source');
      const stamp = fmtIstStamp(q.timestamp);

      if (statusEl) {
        if (q.error) {
          statusEl.textContent = 'Unavailable';
        } else if (q.price === null || q.price === undefined) {
          statusEl.textContent = 'Unavailable';
        } else if (!state.marketOpen) {
          statusEl.textContent = 'Market Closed';
        } else if (q.stale) {
          statusEl.textContent = 'Stale';
        } else {
          statusEl.textContent = 'Live · 2s';
        }
      }

      if (sourceEl) {
        if (q.error) {
          sourceEl.textContent = 'source error';
        } else if (q.price === null || q.price === undefined) {
          sourceEl.textContent = 'no price data';
        } else if (!state.marketOpen) {
          sourceEl.textContent = stamp ? 'last updated ' + stamp : 'last close';
        } else {
          sourceEl.textContent = stamp ? 'source ' + stamp : 'source live';
        }
      }

      if (q.price === null || q.price === undefined) {
        if (priceEl) priceEl.textContent = 'data unavailable';
        if (chgEl) chgEl.textContent = '—';
        if (prevEl) prevEl.textContent = 'prev close —';
        return;
      }

      const targetPrice = Number(q.price);
      if (priceEl) {
        if (state.marketOpen && prevKnown !== null && prevKnown !== targetPrice) {
          glideValue('quote:' + sym, Number(prevKnown), targetPrice, function (v) {
            priceEl.textContent = fmtNum(v, 2);
          });
        } else {
          priceEl.textContent = fmtNum(targetPrice, 2);
        }
      }

      if (chgEl && q.change_pct !== null && q.change_pct !== undefined) {
        chgEl.textContent = fmtPct(q.change_pct);
        chgEl.className = 'index-change price-mono ' + (Number(q.change_pct) >= 0 ? 'up' : 'down');
      }
      if (prevEl && q.prev_close !== null && q.prev_close !== undefined) {
        let prevText = 'prev close ' + fmtNum(q.prev_close, 2);
        if (!state.marketOpen) {
          prevText += stamp ? ' · last updated ' + stamp : ' · last close';
        }
        prevEl.textContent = prevText;
      }
      if (cardEl && q.change_pct !== null && q.change_pct !== undefined) {
        cardEl.classList.toggle('neg', Number(q.change_pct) < 0);
      }
    });

    // Update Market Mood Strip, ORB live state badges, Paper MTM & Smart Alerts
    updateMoodFromQuotes();
    syncOrbStatesWithLivePrices();
    refreshPaperMtm();
    if (state.marketOpen) evaluateSmartAlerts();
  }

  function pollQuotesOnce() {
    if (state.inFlight.quotes) return Promise.resolve();
    state.inFlight.quotes = true;
    return fetchJson('/api/quotes', 10000)
      .then(function (data) {
        state.inFlight.quotes = false;
        if (data && Array.isArray(data.quotes) && data.quotes.length) {
          applyQuotesSnapshot(data.quotes);
          if (state.marketOpen && typeof global.bhanuMarkLive === 'function') {
            global.bhanuMarkLive();
          }
        }
      })
      .catch(function () {
        state.inFlight.quotes = false;
      });
  }

  function pollLiveIndexTicks() {
    if (!state.marketOpen || state.inFlight.live) return;
    const pairs = [
      ['chart-nifty', '^NSEI'],
      ['chart-bank', '^NSEBANK'],
      ['chart-sensex', '^BSESN']
    ];
    const active = pairs.filter(function (pair) { return Boolean($(pair[0])); });
    if (!active.length) return;

    state.inFlight.live = true;
    const jobs = active.map(function (pair) {
      const cid = pair[0], sym = pair[1];
      return fetchJson('/api/live?symbol=' + encodeURIComponent(sym), 6000)
        .then(function (p) {
          if (!p || p.error || p.ltp === null || p.ltp === undefined) return;
          const nextVal = Number(p.ltp);
          const nextTime = p.time || Math.floor(Date.now() / 1000);
          state.liveTicks[sym] = {
            ltp: nextVal,
            prev: p.prev,
            change_pct: p.change_pct,
            time: nextTime
          };
          const stamp = $(cid + '-stamp');
          if (stamp) {
            stamp.dataset.tick = String(nextTime);
            if (typeof global.bhanuTickLabel === 'function') global.bhanuTickLabel(stamp);
          }

          if (state.canvasCharts[cid]) {
            updateCanvasChartLastPoint(cid, nextVal, nextTime);
          }
          const lw = (global.bhanuLines || {})[cid];
          if (lw && lw.series) {
            const fromVal = lw.displayedValue != null ? lw.displayedValue : (lw.lastValue != null ? lw.lastValue : nextVal);
            if (nextTime > (lw.lastTime || 0)) lw.lastTime = nextTime;
            lw.lastValue = nextVal;
            glideValue('lw:' + cid, fromVal, nextVal, function (v) {
              lw.displayedValue = v;
              try { lw.series.update({ time: lw.lastTime, value: v }); } catch (e) {}
            });
          }
          if (typeof global.bhanuSparkFor === 'function' && Array.isArray(p.line)) {
            global.bhanuSparkFor(sym, p.line);
          }
        })
        .catch(function () { /* keep last good value */ });
    });

    Promise.all(jobs).then(function () {
      state.inFlight.live = false;
      syncOrbStatesWithLivePrices();
    });
  }

  function runTwoSecondLivePoll() {
    if (document.hidden || !state.marketOpen) return;
    pollQuotesOnce();
    pollLiveIndexTicks();
  }

  function updateMoodFromQuotes() {
    const nifty = state.quotes['^NSEI'];
    const bank = state.quotes['^NSEBANK'];
    const badge = $('mood-status-badge');
    const niftyCell = $('mood-nifty-chg');
    const bankCell = $('mood-bank-chg');
    const guideEl = $('mood-guidance');

    if (niftyCell && nifty && nifty.change_pct != null) {
      niftyCell.textContent = fmtPct(nifty.change_pct);
      niftyCell.className = Number(nifty.change_pct) >= 0 ? 'up mono' : 'down mono';
    }
    if (bankCell && bank && bank.change_pct != null) {
      bankCell.textContent = fmtPct(bank.change_pct);
      bankCell.className = Number(bank.change_pct) >= 0 ? 'up mono' : 'down mono';
    }

    if (!badge) return;
    if (!state.marketOpen) {
      badge.textContent = 'MARKET CLOSED · FROZEN';
      badge.className = 'pill down';
      if (guideEl) guideEl.textContent = 'Market band hai — last session ka data dikh raha hai.';
      return;
    }
    const chg = nifty && nifty.change_pct != null ? Number(nifty.change_pct) : 0;
    if (chg >= 0.35) {
      badge.textContent = 'TRENDING BULLISH';
      badge.className = 'pill up';
      if (guideEl) guideEl.textContent = 'Bullish trend — dips par CALL setups focus karein.';
    } else if (chg <= -0.35) {
      badge.textContent = 'BEARISH PRESSURE';
      badge.className = 'pill down';
      if (guideEl) guideEl.textContent = 'Bearish bias — rallies par PUT setups aur strict SL.';
    } else {
      badge.textContent = 'RANGE / CHOPPY';
      badge.className = 'pill';
      if (guideEl) guideEl.textContent = 'Rangebound market — quantity half rakhein, ORB breakout ka wait karein.';
    }
  }

  // =========================================================================
  // 6. SMOOTH 60FPS CANVAS CHART ENGINE (S/R, ENTRY, SL, TARGETS, ORB, VWAP)
  // =========================================================================
  function drawRoundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);
    ctx.quadraticCurveTo(x + w, y, x + w, y + r);
    ctx.lineTo(x + w, y + h - r);
    ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    ctx.lineTo(x + r, y + h);
    ctx.quadraticCurveTo(x, y + h, x, y + h - r);
    ctx.lineTo(x, y + r);
    ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.closePath();
  }

  function mountCanvasChart(canvasId, config) {
    const canvas = $(canvasId);
    if (!canvas) return null;
    state.canvasCharts[canvasId] = {
      canvas: canvas,
      type: config.type || 'candles',
      candles: Array.isArray(config.candles) ? config.candles.slice() : [],
      line: Array.isArray(config.line) ? config.line.slice() : [],
      vwap: Array.isArray(config.vwap) ? config.vwap.slice() : [],
      levels: Array.isArray(config.levels) ? config.levels.slice() : [],
      overlays: Object.assign({
        vwap: true, ema20: true, levels: true, volume: true
      }, config.overlays || {})
    };
    renderCanvasChart(canvasId);
    return state.canvasCharts[canvasId];
  }

  function updateCanvasChartLastPoint(canvasId, nextVal, nextTime) {
    const chart = state.canvasCharts[canvasId];
    if (!chart) return;
    if (chart.type === 'line' && chart.line.length) {
      const last = chart.line[chart.line.length - 1];
      const fromVal = Number(last.value);
      if (nextTime && nextTime > last.time) {
        chart.line.push({ time: nextTime, value: fromVal });
      }
      glideValue('canvas:' + canvasId, fromVal, nextVal, function (v) {
        chart.line[chart.line.length - 1].value = v;
        renderCanvasChart(canvasId);
      });
    } else if (chart.type === 'candles' && chart.candles.length) {
      const last = chart.candles[chart.candles.length - 1];
      const fromVal = Number(last.close);
      glideValue('canvas:' + canvasId, fromVal, nextVal, function (v) {
        last.close = v;
        if (v > last.high) last.high = v;
        if (v < last.low) last.low = v;
        renderCanvasChart(canvasId);
      });
    }
  }

  function renderCanvasChart(canvasId) {
    const chart = state.canvasCharts[canvasId];
    if (!chart || !chart.canvas) return;
    const canvas = chart.canvas;
    const rect = canvas.parentElement ? canvas.parentElement.getBoundingClientRect() : canvas.getBoundingClientRect();
    const W = Math.floor(rect.width);
    const H = Math.floor(rect.height || 260);
    if (W < 40 || H < 30) return;

    const dpr = Math.min(global.devicePixelRatio || 1, 2);
    if (canvas.width !== Math.floor(W * dpr) || canvas.height !== Math.floor(H * dpr)) {
      canvas.width = Math.floor(W * dpr);
      canvas.height = Math.floor(H * dpr);
    }

    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    const pal = getCanvasPalette();

    if (chart.type === 'line') {
      drawLineCanvas(ctx, W, H, chart, pal);
    } else {
      drawCandlesCanvas(ctx, W, H, chart, pal);
    }
  }

  function drawLineCanvas(ctx, W, H, chart, pal) {
    const pts = chart.line;
    if (!pts.length) return;
    const padL = 10, padR = 58, padT = 12, padB = 14;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;
    const vals = pts.map(function (p) { return Number(p.value); });
    const rawMin = Math.min.apply(null, vals);
    const rawMax = Math.max.apply(null, vals);
    const span = Math.max(1, rawMax - rawMin);
    const yMin = rawMin - span * 0.15;
    const yMax = rawMax + span * 0.15;
    const xOf = function (i) { return padL + (i / (pts.length - 1 || 1)) * plotW; };
    const yOf = function (v) { return padT + (1 - (v - yMin) / (yMax - yMin || 1)) * plotH; };

    const up = vals[vals.length - 1] >= vals[0];
    const col = up ? pal.bull : pal.bear;

    const grad = ctx.createLinearGradient(0, padT, 0, H - padB);
    grad.addColorStop(0, up ? pal.bullSoft : pal.bearSoft);
    grad.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.beginPath();
    ctx.moveTo(xOf(0), H - padB);
    pts.forEach(function (p, i) { ctx.lineTo(xOf(i), yOf(p.value)); });
    ctx.lineTo(xOf(pts.length - 1), H - padB);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    ctx.strokeStyle = col;
    ctx.lineWidth = 1.8;
    ctx.beginPath();
    pts.forEach(function (p, i) {
      if (i === 0) ctx.moveTo(xOf(i), yOf(p.value));
      else ctx.lineTo(xOf(i), yOf(p.value));
    });
    ctx.stroke();

    const lastIdx = pts.length - 1;
    const lastX = xOf(lastIdx);
    const lastY = yOf(vals[lastIdx]);
    ctx.shadowColor = col;
    ctx.shadowBlur = 8;
    ctx.fillStyle = col;
    ctx.beginPath();
    ctx.arc(lastX, lastY, 4, 0, Math.PI * 2);
    ctx.fill();
    ctx.shadowBlur = 0;

    const pillY = Math.max(4, Math.min(H - 20, lastY - 8));
    drawRoundRect(ctx, W - padR + 3, pillY, padR - 5, 16, 4);
    ctx.fill();
    ctx.fillStyle = pal.pillText;
    ctx.font = '700 9px "JetBrains Mono", ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace';
    ctx.textAlign = 'center';
    ctx.fillText(vals[lastIdx] >= 10000 ? vals[lastIdx].toFixed(0) : vals[lastIdx].toFixed(1), W - padR + (padR - 2) / 2, pillY + 11);
  }

  function drawCandlesCanvas(ctx, W, H, chart, pal) {
    const candles = chart.candles;
    if (!candles.length) return;

    const padL = 12, padR = 66, padT = 16, padB = 22;
    const plotW = W - padL - padR;
    const priceH = H - padT - padB;
    const lows = candles.map(function (c) { return Number(c.low); });
    const highs = candles.map(function (c) { return Number(c.high); });
    chart.levels.forEach(function (lv) {
      if (lv.price != null) {
        lows.push(Number(lv.price));
        highs.push(Number(lv.price));
      }
    });
    const rawMin = Math.min.apply(null, lows);
    const rawMax = Math.max.apply(null, highs);
    const span = Math.max(1, rawMax - rawMin);
    const yMin = rawMin - span * 0.12;
    const yMax = rawMax + span * 0.12;
    const yOf = function (p) { return padT + (1 - (p - yMin) / (yMax - yMin || 1)) * priceH; };
    const n = candles.length;
    const slotW = plotW / n;
    const bodyW = Math.max(2.5, Math.min(10, Math.floor(slotW * 0.64)));

    ctx.strokeStyle = pal.grid;
    ctx.fillStyle = pal.axisText;
    ctx.font = '600 9px "JetBrains Mono", ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace';
    ctx.textAlign = 'left';
    for (let g = 0; g <= 4; g++) {
      const y = padT + (g / 4) * priceH;
      const val = yMax - (g / 4) * (yMax - yMin);
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(W - padR, y);
      ctx.stroke();
      ctx.fillText(val >= 10000 ? val.toFixed(0) : val.toFixed(1), W - padR + 4, y + 3);
    }

    if (chart.overlays.levels) {
      chart.levels.forEach(function (lv) {
        if (lv.price == null) return;
        const y = Math.max(padT + 4, Math.min(H - padB - 4, yOf(Number(lv.price))));
        const col = lv.color || pal.blue;
        ctx.save();
        if (lv.dashed !== false) ctx.setLineDash([4, 3]);
        ctx.strokeStyle = col;
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.moveTo(padL, y);
        ctx.lineTo(W - padR, y);
        ctx.stroke();
        ctx.restore();

        ctx.fillStyle = col;
        ctx.font = '700 8px "JetBrains Mono", ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace';
        ctx.textAlign = 'left';
        ctx.fillText(String(lv.label || ''), padL + 3, y - 3);

        const tagY = Math.max(padT, Math.min(H - padB - 13, y - 6.5));
        ctx.fillStyle = col;
        drawRoundRect(ctx, W - padR + 2, tagY, padR - 4, 13, 3);
        ctx.fill();
        ctx.fillStyle = pal.pillText;
        ctx.textAlign = 'center';
        const pNum = Number(lv.price);
        ctx.fillText(pNum >= 10000 ? pNum.toFixed(0) : pNum.toFixed(1), W - padR + padR / 2, tagY + 9.5);
      });
    }

    candles.forEach(function (c, i) {
      const cx = padL + (i + 0.5) * slotW;
      const isBull = Number(c.close) >= Number(c.open);
      const col = isBull ? pal.bull : pal.bear;
      ctx.strokeStyle = col;
      ctx.lineWidth = 1.15;
      ctx.beginPath();
      ctx.moveTo(cx, yOf(Number(c.high)));
      ctx.lineTo(cx, yOf(Number(c.low)));
      ctx.stroke();

      const yO = yOf(Number(c.open));
      const yC = yOf(Number(c.close));
      ctx.fillStyle = col;
      ctx.fillRect(cx - bodyW / 2, Math.min(yO, yC), bodyW, Math.max(1.8, Math.abs(yC - yO)));
    });

    if (chart.overlays.vwap && chart.vwap.length) {
      ctx.strokeStyle = pal.blue;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      chart.vwap.forEach(function (v, i) {
        const cx = padL + (i + 0.5) * (plotW / chart.vwap.length);
        const y = yOf(Number(v.value));
        if (i === 0) ctx.moveTo(cx, y);
        else ctx.lineTo(cx, y);
      });
      ctx.stroke();
    }

    const last = candles[n - 1];
    const lastC = Number(last.close);
    const yL = yOf(lastC);
    const lCol = lastC >= Number(last.open) ? pal.bull : pal.bear;
    ctx.save();
    ctx.setLineDash([2, 2]);
    ctx.strokeStyle = lCol;
    ctx.beginPath();
    ctx.moveTo(padL, yL);
    ctx.lineTo(W - padR, yL);
    ctx.stroke();
    ctx.restore();

    const pillY = Math.max(2, Math.min(H - padB - 16, yL - 8));
    ctx.fillStyle = lCol;
    drawRoundRect(ctx, W - padR + 2, pillY, padR - 4, 16, 4);
    ctx.fill();
    ctx.fillStyle = pal.pillText;
    ctx.font = '800 8.5px "JetBrains Mono", ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace';
    ctx.textAlign = 'center';
    ctx.fillText(lastC >= 10000 ? lastC.toFixed(0) : lastC.toFixed(1), W - padR + padR / 2, pillY + 11);
  }

  // =========================================================================
  // 6b. MONTE CARLO HISTOGRAM CANVAS CHART (R1-E1: Mean line + ±1σ / ±2σ zones)
  // =========================================================================
  function renderMcHistCanvas(canvasId) {
    const canvas = $(canvasId);
    if (!canvas) return;

    let values = [];
    let mean = 0;
    let std = 0;
    let buckets = [];
    try {
      values = JSON.parse(canvas.getAttribute('data-values') || '[]');
      mean = parseFloat(canvas.getAttribute('data-mean') || '0');
      std = parseFloat(canvas.getAttribute('data-std') || '0');
      buckets = JSON.parse(canvas.getAttribute('data-buckets') || '[]');
    } catch (e) {
      return;
    }

    const dpr = Math.min(global.devicePixelRatio || 1, 2);
    const rect = canvas.parentElement ? canvas.parentElement.getBoundingClientRect() : canvas.getBoundingClientRect();
    const W = Math.floor(Math.min(600, rect.width || 600));
    const H = 200;

    if (W < 120 || H < 50) return;

    canvas.style.width = W + 'px';
    canvas.style.height = H + 'px';
    if (canvas.width !== Math.floor(W * dpr) || canvas.height !== Math.floor(H * dpr)) {
      canvas.width = Math.floor(W * dpr);
      canvas.height = Math.floor(H * dpr);
    }

    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    const pal = getCanvasPalette();
    ctx.fillStyle = pal.bg;
    ctx.fillRect(0, 0, W, H);

    const padL = 46, padR = 16, padT = 32, padB = 30;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;

    if ((!values.length && !buckets.length) || plotW <= 0 || plotH <= 0) return;

    const bucketCenters = {
      'Stop (-1R)': -1.0,
      '-1R to 0R': -0.5,
      '0R to +1R': 0.5,
      '+1R to +2R': 1.5,
      'Target (+2R)': 2.0
    };

    let minVal = -1.2;
    let maxVal = 2.2;
    if (values.length) {
      let lo = values[0], hi = values[0];
      for (let i = 1; i < values.length; i++) {
        if (values[i] < lo) lo = values[i];
        if (values[i] > hi) hi = values[i];
      }
      minVal = Math.min(lo, mean - 2 * std, -1.0);
      maxVal = Math.max(hi, mean + 2 * std, 2.0);
    }
    const span = Math.max(0.5, maxVal - minVal);
    const xMin = minVal - span * 0.08;
    const xMax = maxVal + span * 0.08;
    const xRange = xMax - xMin;

    const xOf = function (r) {
      return padL + ((r - xMin) / xRange) * plotW;
    };

    // Draw ±2σ and ±1σ shaded zones
    if (std > 0) {
      const sd2L = Math.max(padL, xOf(mean - 2 * std));
      const sd2R = Math.min(W - padR, xOf(mean + 2 * std));
      if (sd2R > sd2L) {
        ctx.fillStyle = 'rgba(37, 99, 235, 0.08)';
        ctx.fillRect(sd2L, padT, sd2R - sd2L, plotH);
      }
      const sd1L = Math.max(padL, xOf(mean - std));
      const sd1R = Math.min(W - padR, xOf(mean + std));
      if (sd1R > sd1L) {
        ctx.fillStyle = 'rgba(37, 99, 235, 0.16)';
        ctx.fillRect(sd1L, padT, sd1R - sd1L, plotH);
      }
    }

    // Horizontal baseline
    ctx.strokeStyle = pal.divider;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(padL, H - padB);
    ctx.lineTo(W - padR, H - padB);
    ctx.stroke();

    // Draw histogram bars
    const maxCount = Math.max.apply(null, buckets.map(function (b) { return b.count || 0; }).concat([1]));
    buckets.forEach(function (b) {
      const center = bucketCenters[b.label] !== undefined ? bucketCenters[b.label] : 0;
      const x = xOf(center);
      const barW = Math.max(14, Math.min(plotW / 6, plotW * 0.14));
      const barH = Math.max(2, Math.floor(((b.count || 0) / maxCount) * (plotH * 0.84)));

      let color = '#2563eb';
      if (String(b.label).indexOf('Target') === 0) color = pal.bull;
      else if (String(b.label).indexOf('Stop') === 0) color = pal.bear;

      const barX = Math.floor(x - barW / 2);
      const barY = H - padB - barH;
      ctx.fillStyle = color;
      ctx.fillRect(barX, barY, barW, barH);

      ctx.fillStyle = pal.axisText;
      ctx.font = '700 8px "JetBrains Mono", ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace';
      ctx.textAlign = 'center';
      ctx.fillText(Number(b.pct || 0).toFixed(1) + '%', x, Math.max(padT + 8, barY - 3));
    });

    // Draw ±1σ dashed lines & ±2σ dotted lines
    if (std > 0) {
      ctx.save();
      ctx.strokeStyle = pal.blue;
      ctx.lineWidth = 1.25;
      ctx.setLineDash([5, 3]);
      [mean - std, mean + std].forEach(function (xVal) {
        const x = xOf(xVal);
        if (x >= padL && x <= W - padR) {
          ctx.beginPath();
          ctx.moveTo(x, padT);
          ctx.lineTo(x, H - padB);
          ctx.stroke();
        }
      });
      ctx.lineWidth = 1;
      ctx.setLineDash([2, 3]);
      [mean - 2 * std, mean + 2 * std].forEach(function (xVal) {
        const x = xOf(xVal);
        if (x >= padL && x <= W - padR) {
          ctx.beginPath();
          ctx.moveTo(x, padT);
          ctx.lineTo(x, H - padB);
          ctx.stroke();
        }
      });
      ctx.restore();
    }

    // Draw Mean vertical line + pill badge
    const meanX = Math.max(padL + 2, Math.min(W - padR - 2, xOf(mean)));
    ctx.save();
    ctx.strokeStyle = '#dc2626';
    ctx.lineWidth = 2.2;
    ctx.beginPath();
    ctx.moveTo(meanX, padT);
    ctx.lineTo(meanX, H - padB);
    ctx.stroke();
    ctx.restore();

    const lblText = 'Mean ' + (mean >= 0 ? '+' : '') + mean.toFixed(2) + 'R';
    const pillW = 68;
    const pillX = Math.max(padL, Math.min(W - padR - pillW, meanX - pillW / 2));
    ctx.fillStyle = '#dc2626';
    drawRoundRect(ctx, pillX, 6, pillW, 15, 4);
    ctx.fill();
    ctx.fillStyle = '#FFFFFF';
    ctx.font = '700 8.5px "JetBrains Mono", ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace';
    ctx.textAlign = 'center';
    ctx.fillText(lblText, pillX + pillW / 2, 16.5);

    // X-axis bucket labels
    ctx.fillStyle = pal.axisText;
    ctx.font = '600 8px "JetBrains Mono", ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace';
    ctx.textAlign = 'center';
    Object.keys(bucketCenters).forEach(function (label) {
      const x = xOf(bucketCenters[label]);
      if (x >= padL && x <= W - padR) {
        ctx.fillText(label, x, H - padB + 15);
      }
    });

    // Y-axis label
    ctx.save();
    ctx.translate(12, padT + plotH / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.textAlign = 'center';
    ctx.fillText('Replays', 0, 0);
    ctx.restore();

    // Compact legend (top-left)
    ctx.fillStyle = 'rgba(37, 99, 235, 0.16)';
    ctx.fillRect(padL + 2, 9, 12, 9);
    ctx.fillStyle = pal.axisText;
    ctx.font = '600 8px "JetBrains Mono", ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace';
    ctx.textAlign = 'left';
    ctx.fillText('±1σ', padL + 17, 16.5);
    ctx.fillStyle = 'rgba(37, 99, 235, 0.08)';
    ctx.fillRect(padL + 44, 9, 12, 9);
    ctx.fillStyle = pal.axisText;
    ctx.fillText('±2σ', padL + 59, 16.5);
  }

  function renderAllMcCanvases(rootEl) {
    const scope = rootEl || document;
    scope.querySelectorAll('canvas[data-buckets]').forEach(function (c) {
      if (c.id) renderMcHistCanvas(c.id);
    });
  }

  // =========================================================================
  // 7. TIMEFRAME SWITCHERS (ORB, LEVELS & STOCK PAGES)
  // =========================================================================
  function switchChartTimeframe(page, symbol, interval, btn) {
    if (btn && btn.parentElement) {
      btn.parentElement.querySelectorAll('.tfbtn').forEach(function (b) {
        b.classList.toggle('on', b === btn);
      });
    }
    const sym = symbol || 'NIFTY';
    const iv = interval || '5m';

    if (page === 'stock' && typeof global.bhanuStockLoad === 'function') {
      global.bhanuStockLoad(sym, iv);
      return;
    }

    const chartId = page === 'orb' ? 'orb-chart' : 'levels-chart';
    const url = page === 'levels'
      ? '/api/levelschart?symbol=' + encodeURIComponent(sym) + '&interval=' + encodeURIComponent(iv)
      : '/api/stock?symbol=' + encodeURIComponent(sym) + '&interval=' + encodeURIComponent(iv);
    const labelEl = $(chartId + '-indicators');
    if (labelEl) labelEl.textContent = 'Loading ' + sym + ' (' + iv + ') candles…';

    fetchAsyncDetail(url)
      .then(function (data) {
        const chartPayload = (data && data.chart) ? data.chart : data;
        if (!chartPayload || chartPayload.error || !chartPayload.candles || !chartPayload.candles.length) {
          throw new Error((chartPayload && chartPayload.error) || (data && data.error) || 'No candle data for ' + iv);
        }
        if (typeof global.bhanuCandleUpdate === 'function' && (global.bhanuCandles || {})[chartId]) {
          global.bhanuCandleUpdate(chartId, chartPayload);
        } else if (typeof global.bhanuCandleRender === 'function') {
          const el = $(chartId);
          if (el) el.innerHTML = '';
          global.bhanuCandleRender(chartId, chartPayload, null);
        }
        showToast(sym + ' switched to ' + iv + ' timeframe');
      })
      .catch(function (err) {
        if (labelEl) labelEl.textContent = 'Timeframe unavailable · ' + err.message;
        showToast('Could not load ' + iv + ': ' + err.message);
      });
  }

  // =========================================================================
  // 8. BOT PAGE: LIVE AUTO-SCAN RIBBON, RE-SCAN & BEST SETUP
  // =========================================================================
  function updateBotPollerMetrics(st) {
    if (!st) return;
    const scannedEl = $('bot-scanned-count');
    const hitsEl = $('bot-hits-count');
    const replayEl = $('bot-replays-count');
    if (scannedEl) scannedEl.textContent = String(st.symbols_scanned || 0);
    if (hitsEl) hitsEl.textContent = String(st.orb_hits || 0);
    if (replayEl) replayEl.textContent = Number(st.timemachine_replays || 0).toLocaleString('en-IN');
  }

  function tickBotAutoRibbon() {
    if (!state.marketOpen) return;
    const tickerEl = $('bot-live-ticker');
    const pulseEl = $('bot-scan-cycle');
    if (!tickerEl) return;

    const pool = state.bot.symbols.length
      ? state.bot.symbols
      : ['NIFTY', 'BANKNIFTY', 'RELIANCE', 'HDFCBANK', 'TCS', 'INFY', 'ICICIBANK', 'SBIN', 'HAL', 'LT'];
    state.bot.tickerIdx = (state.bot.tickerIdx + 1) % pool.length;
    if (state.bot.tickerIdx === 0) state.bot.scanCount += 1;

    const a = pool[state.bot.tickerIdx];
    const b = pool[(state.bot.tickerIdx + 1) % pool.length];
    const c = pool[(state.bot.tickerIdx + 2) % pool.length];
    tickerEl.textContent = 'AUTO-SCANNING: ' + a + ' → ' + b + ' → ' + c;
    if (pulseEl) pulseEl.textContent = 'SCAN #' + (state.bot.scanCount + 1);
  }

  function loadBotPageData() {
    const bestSlot = $('bot-best-setup-slot');
    const listSlot = $('bot-top-setups-list');
    if (!bestSlot && !listSlot) return;

    Promise.all([
      fetchJson('/api/robot', 12000).catch(function () { return null; }),
      fetchJson('/api/orb?limit=10', 12000).catch(function () { return null; }),
      fetchJson('/api/tmmini', 12000).catch(function () { return null; })
    ]).then(function (results) {
      const robot = results[0];
      const orb = results[1];
      const tm = results[2];

      if (bestSlot && robot && robot.html) {
        bestSlot.innerHTML = robot.html;
      }
      if (listSlot && orb && orb.html) {
        listSlot.innerHTML = orb.html;
        syncOrbStatesWithLivePrices();
      }
      const probSlot = $('bot-tm-prob-slot');
      if (probSlot && tm && tm.html) {
        probSlot.innerHTML = tm.html;
      }

      if (orb && orb.html) {
        const matches = orb.html.match(/symbol=([A-Z0-9.^]+)/g) || [];
        const syms = matches.map(function (m) { return m.replace('symbol=', ''); });
        if (syms.length) state.bot.symbols = Array.from(new Set(syms));
      }
    });
  }

  function triggerBotRescan() {
    if (state.bot.scanning) return;
    if (!state.marketOpen) {
      showToast('Market Closed — Showing last session scan snapshot');
      loadBotPageData();
      return;
    }
    state.bot.scanning = true;
    const fill = $('bot-scan-fill');
    const pctEl = $('bot-scan-pct');
    let pct = 10;
    const iv = setInterval(function () {
      pct = Math.min(100, pct + 18);
      if (fill) fill.style.width = pct + '%';
      if (pctEl) pctEl.textContent = pct + '%';
      if (pct >= 100) {
        clearInterval(iv);
        state.bot.scanning = false;
        state.bot.scanCount += 1;
        loadBotPageData();
        showToast('Bot scan refreshed from live NSE data');
      }
    }, 110);
  }

  // =========================================================================
  // 9. TIME MACHINE (10,000 REPLAYS TRADE-INPUT FORM + CANVAS DRAW)
  // =========================================================================
  function submitTimeMachineCheck(ev) {
    if (ev && ev.preventDefault) ev.preventDefault();
    const form = $('tm-check-form');
    const out = $('tm-check-result');
    if (!form || !out) return false;

    const params = new URLSearchParams({
      symbol: form.symbol.value,
      entry: form.entry.value,
      stop: form.stop.value,
      target: form.target.value,
      direction: form.direction.value
    });

    out.innerHTML = '<p class="loading">10,000 replays chal rahe hain (real 5m bars par)…</p>';
    fetchAsyncDetail('/api/timemachine-check?' + params.toString())
      .then(function (data) {
        if (!data || !data.ok) {
          out.innerHTML = '<p class="feed-message">Check nahi ho paya — ' +
            escHtml((data && data.error) || 'invalid input') + '</p>';
          return;
        }
        out.innerHTML = data.html || '<p class="muted">Replay complete.</p>';
        // innerHTML does not execute inline <script> tags, so draw MC canvases directly
        renderAllMcCanvases(out);
        const mc = data.monte_carlo || {};
        if (mc.available) {
          showToast(
            data.symbol + ' ' + optLabel(data.direction) +
            ': Target-first ' + Number(mc.target_first_pct).toFixed(1) +
            '% · Stop-first ' + Number(mc.stop_first_pct).toFixed(1) +
            '% · Avg ' + (mc.avg_r >= 0 ? '+' : '') + Number(mc.avg_r).toFixed(2) + 'R'
          );
        }
      })
      .catch(function (err) {
        out.innerHTML = '<p class="feed-message">Time Machine unavailable — ' + escHtml(err.message) + '</p>';
      });
    return false;
  }

  function observeLazyMcCanvases() {
    if (typeof MutationObserver === 'undefined') return;
    ['timemachine-body', 'tm-check-result'].forEach(function (id) {
      const host = $(id);
      if (!host) return;
      const obs = new MutationObserver(function () {
        renderAllMcCanvases(host);
      });
      obs.observe(host, { childList: true, subtree: true });
    });
    // Also draw when a collapsed <details> containing a net histogram is opened
    document.addEventListener('toggle', function (e) {
      if (e.target && e.target.tagName === 'DETAILS' && e.target.open) {
        renderAllMcCanvases(e.target);
      }
    }, true);
  }

  // =========================================================================
  // 10. PAPER TRADING DESK, RISK GUARDIAN, CHECKLIST & AUTO TRADE JOURNAL
  // =========================================================================
  function initPaperState() {
    const saved = loadStorage(STORAGE_KEYS.paper, null);
    if (saved && Array.isArray(saved.positions)) {
      state.paper = saved;
    }
    state.journal = loadStorage(STORAGE_KEYS.journal, []);
    state.killSwitch = Boolean(loadStorage(STORAGE_KEYS.killSwitch, false));
    state.alerts = loadStorage(STORAGE_KEYS.alerts, []);
  }

  function persistPaperState() {
    saveStorage(STORAGE_KEYS.paper, state.paper);
  }

  const paperLive = { symbol: null, price: null };

  function updatePaperLivePrice() {
    const symEl = $('pro-paper-symbol');
    const priceEl = $('pro-paper-live-price');
    if (!symEl) return;
    const symbol = (symEl.value || 'NIFTY').trim().toUpperCase();
    if (priceEl) priceEl.textContent = 'Live price: load ho raha hai…';
    fetchJson('/api/quote?symbol=' + encodeURIComponent(symbol), 15000).then(function (q) {
      if (!q || q.error || q.price === null || q.price === undefined) {
        throw new Error((q && q.error) || 'no live price');
      }
      paperLive.symbol = symbol;
      paperLive.price = Number(q.price);
      if (priceEl) {
        const pct = (q.change_pct === null || q.change_pct === undefined)
          ? '' : ' (' + (q.change_pct >= 0 ? '+' : '') + Number(q.change_pct).toFixed(2) + '%)';
        priceEl.textContent = 'Live price: ₹' + fmtNum(Number(q.price)) + pct;
      }
    }).catch(function () {
      paperLive.symbol = null;
      paperLive.price = null;
      if (priceEl) priceEl.textContent = 'Live price: unavailable — Entry ₹ khud daalein.';
    });
  }

  function paperSymbolChanged() {
    updatePaperLivePrice();
  }

  function resolveLivePriceForSymbol(sym) {
    const s = String(sym || '').toUpperCase();
    if (!s) return null;
    if (paperLive.symbol === s && paperLive.price) {
      return Number(paperLive.price);
    }
    if (s.indexOf('BANKNIFTY') !== -1 || s === '^NSEBANK') {
      if (state.liveTicks['^NSEBANK'] && state.liveTicks['^NSEBANK'].ltp) return Number(state.liveTicks['^NSEBANK'].ltp);
      if (state.quotes['^NSEBANK'] && state.quotes['^NSEBANK'].price) return Number(state.quotes['^NSEBANK'].price);
    }
    if (s.indexOf('SENSEX') !== -1 || s === '^BSESN') {
      if (state.liveTicks['^BSESN'] && state.liveTicks['^BSESN'].ltp) return Number(state.liveTicks['^BSESN'].ltp);
      if (state.quotes['^BSESN'] && state.quotes['^BSESN'].price) return Number(state.quotes['^BSESN'].price);
    }
    if (s.indexOf('NIFTY') !== -1 || s === '^NSEI') {
      if (state.liveTicks['^NSEI'] && state.liveTicks['^NSEI'].ltp) return Number(state.liveTicks['^NSEI'].ltp);
      if (state.quotes['^NSEI'] && state.quotes['^NSEI'].price) return Number(state.quotes['^NSEI'].price);
    }
    if (state.quotes[s] && state.quotes[s].price) return Number(state.quotes[s].price);
    return null;
  }

  function placePaperOrder(side) {
    if (state.killSwitch) {
      showToast('Kill-Switch ACTIVE — Aaj aur trade mat karo, pehle unlock karo.');
      return;
    }
    const symEl = $('pro-paper-symbol');
    const qtyEl = $('pro-paper-qty');
    const priceEl = $('pro-paper-price');
    const slEl = $('pro-paper-sl');
    const tgtEl = $('pro-paper-target');

    const symbol = (symEl && symEl.value ? symEl.value : 'NIFTY').trim().toUpperCase();
    const qty = Math.max(1, parseInt(qtyEl ? qtyEl.value : '50', 10) || 50);
    const livePx = resolveLivePriceForSymbol(symbol);
    const entry = parseFloat(priceEl && priceEl.value ? priceEl.value : '') || livePx;

    if (!entry || isNaN(entry) || entry <= 0) {
      showToast('Entry price daalo (ya live quote load hone ka wait karein).');
      return;
    }

    const isPutOrSell = (side === 'BUY PUT' || side === 'SELL');
    const slInput = slEl && slEl.value ? parseFloat(slEl.value) : null;
    if (slInput === null || isNaN(slInput)) {
      if (!confirm('Stop Loss set nahi hai. Kya aap bina SL ke order place karna chahte hain?')) {
        return;
      }
    }
    const stop = slInput || Number((isPutOrSell ? entry * 1.005 : entry * 0.995).toFixed(2));
    const target = parseFloat(tgtEl && tgtEl.value ? tgtEl.value : '') ||
      Number((isPutOrSell ? entry * 0.99 : entry * 1.01).toFixed(2));

    const checks = document.querySelectorAll('.pro-pre-chk');
    const checked = document.querySelectorAll('.pro-pre-chk:checked').length;

    const charges = Math.round(40 + qty * entry * 0.00018);
    const risk = Math.abs(entry - stop) * qty;

    if (!confirm('CONFIRM ORDER:\n' + side + ' ' + symbol + ' x' + qty + '\nEntry: ₹' + fmtNum(entry) +
        '\nEst. Charges: ₹' + fmtNum(charges) + '\nRisk: ₹' + fmtNum(risk) + '\n\nProceed?')) {
      return;
    }

    const pos = {
      id: 'PT-' + String(state.paper.nextId++).padStart(4, '0'),
      symbol: symbol,
      side: side,
      qty: qty,
      entry: Number(entry.toFixed(2)),
      ltp: Number(entry.toFixed(2)),
      stop: stop,
      target: target,
      openedAt: clockFmt.format(new Date()) + ' IST'
    };

    state.paper.positions.unshift(pos);
    persistPaperState();
    renderPaperDesk();

    const note = (checks.length && checked < checks.length)
      ? ' (' + checked + '/' + checks.length + ' checklist)'
      : ' (Disciplined)';
    showToast('Paper Order: ' + side + ' ' + symbol + ' x' + qty + ' @ ₹' + fmtNum(entry) + note);
  }

  function squareOffPosition(posId) {
    const idx = state.paper.positions.findIndex(function (p) { return p.id === posId; });
    if (idx === -1) return;
    const p = state.paper.positions[idx];
    const exitPx = p.ltp || p.entry;
    const mult = (p.side === 'SELL' || p.side === 'BUY PUT') ? -1 : 1;
    const pnl = Number(((exitPx - p.entry) * mult * p.qty).toFixed(2));

    state.paper.realized = Number((state.paper.realized + pnl).toFixed(2));
    state.paper.positions.splice(idx, 1);
    persistPaperState();

    const risk = Math.max(1, Math.abs(p.entry - p.stop) * p.qty);
    const rMult = (pnl / risk).toFixed(2);
    state.journal.unshift({
      id: p.id,
      time: clockFmt.format(new Date()) + ' IST',
      symbol: p.symbol,
      side: p.side,
      qty: p.qty,
      entry: p.entry,
      exit: exitPx,
      pnl: pnl,
      r: (Number(rMult) >= 0 ? '+' : '') + rMult + 'R',
      tag: pnl >= 0 ? 'Rule Followed' : 'Early Exit'
    });
    saveStorage(STORAGE_KEYS.journal, state.journal.slice(0, 100));

    renderPaperDesk();
    renderJournal();
    showToast('Squared off ' + p.symbol + ' (' + (pnl >= 0 ? '+' : '') + '₹' + fmtNum(pnl) + ') · Saved to Journal');
  }

  function refreshPaperMtm() {
    let totalMtm = 0;
    state.paper.positions.forEach(function (p) {
      const livePx = resolveLivePriceForSymbol(p.symbol);
      if (livePx !== null) {
        p.ltp = livePx;
      }
      const mult = (p.side === 'SELL' || p.side === 'BUY PUT') ? -1 : 1;
      const pnl = (p.ltp - p.entry) * mult * p.qty;
      totalMtm += pnl;

      const ltpEl = $('pro-pos-ltp-' + p.id);
      const pnlEl = $('pro-pos-pnl-' + p.id);
      if (ltpEl) ltpEl.textContent = fmtNum(p.ltp, 2);
      if (pnlEl) {
        pnlEl.textContent = (pnl >= 0 ? '+' : '') + '₹' + fmtNum(pnl, 2);
        pnlEl.className = 'mono ' + (pnl >= 0 ? 'up' : 'down');
      }
    });

    const mtmEl = $('pro-paper-mtm');
    const relEl = $('pro-paper-realized');
    const cntEl = $('pro-paper-open-count');
    if (mtmEl) {
      mtmEl.textContent = (totalMtm >= 0 ? '+' : '') + '₹' + fmtNum(totalMtm, 2);
      mtmEl.className = 'mono ' + (totalMtm >= 0 ? 'up' : 'down');
    }
    if (relEl) {
      relEl.textContent = (state.paper.realized >= 0 ? '+' : '') + '₹' + fmtNum(state.paper.realized, 2);
      relEl.className = 'mono ' + (state.paper.realized >= 0 ? 'up' : 'down');
    }
    if (cntEl) cntEl.textContent = String(state.paper.positions.length);
  }

  function renderPaperDesk() {
    const list = $('pro-paper-positions');
    if (!list) return;
    if (!state.paper.positions.length) {
      list.innerHTML = '<p class="empty-state">Koi open paper position nahi hai — upar se order place karein.</p>';
      refreshPaperMtm();
      return;
    }

    list.innerHTML = state.paper.positions.map(function (p) {
      const mult = (p.side === 'SELL' || p.side === 'BUY PUT') ? -1 : 1;
      const pnl = (p.ltp - p.entry) * mult * p.qty;
      return (
        '<div class="tpanel" style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap">' +
          '<div>' +
            '<strong>' + escHtml(p.symbol) + '</strong> ' +
            '<span class="' + optClass(p.side) + '">' + escHtml(p.side) + '</span>' +
            '<div class="muted mono" style="font-size:11.5px;margin-top:2px">' +
              'Qty ' + p.qty + ' · Entry ₹' + fmtNum(p.entry) +
              ' · LTP ₹<span id="pro-pos-ltp-' + escHtml(p.id) + '">' + fmtNum(p.ltp) + '</span>' +
              ' · SL ' + fmtNum(p.stop) + ' · TP ' + fmtNum(p.target) +
            '</div>' +
          '</div>' +
          '<div style="text-align:right">' +
            '<div class="mono ' + (pnl >= 0 ? 'up' : 'down') + '" id="pro-pos-pnl-' + escHtml(p.id) + '" style="font-weight:800">' +
              (pnl >= 0 ? '+' : '') + '₹' + fmtNum(pnl) +
            '</div>' +
            '<button type="button" class="btn-ghost" style="margin-top:4px;padding:4px 9px;font-size:11px" ' +
              'onclick="BhanuPro.squareOff(\'' + escHtml(p.id) + '\')">1-Tap Square Off</button>' +
          '</div>' +
        '</div>'
      );
    }).join('');
    refreshPaperMtm();
  }

  function calcRiskGuardian() {
    const cap = parseFloat(($('rg-capital') || {}).value) || 100000;
    const pct = parseFloat(($('rg-risk-pct') || {}).value) || 1.0;
    const entry = parseFloat(($('rg-entry') || {}).value) || 0;
    const sl = parseFloat(($('rg-sl') || {}).value) || 0;

    const maxRisk = Math.round(cap * (pct / 100));
    const perUnit = Math.abs(entry - sl);
    const qty = perUnit > 0 ? Math.max(0, Math.floor(maxRisk / perUnit)) : 0;
    const charges = qty > 0 ? Math.round(40 + qty * entry * 0.00018) : 0;

    if ($('rg-qty-out')) $('rg-qty-out').textContent = qty + ' units';
    if ($('rg-risk-out')) $('rg-risk-out').textContent = '₹' + maxRisk.toLocaleString('en-IN');
    if ($('rg-charges-out')) $('rg-charges-out').textContent = '₹' + charges.toLocaleString('en-IN');
  }

  function toggleKillSwitch(forceVal) {
    state.killSwitch = typeof forceVal === 'boolean' ? forceVal : !state.killSwitch;
    saveStorage(STORAGE_KEYS.killSwitch, state.killSwitch);
    const banner = $('pro-kill-banner');
    if (banner) banner.classList.toggle('active', state.killSwitch);
    ['btn-buy-call', 'btn-buy-put', 'btn-sell'].forEach(function (id) {
      const b = $(id);
      if (b) b.disabled = state.killSwitch;
    });
    showToast(state.killSwitch
      ? 'Daily Loss Kill-Switch ACTIVE — Orders locked'
      : 'Kill-Switch unlocked');
  }

  function updateChecklistBadge() {
    const all = document.querySelectorAll('.pro-pre-chk').length;
    const checked = document.querySelectorAll('.pro-pre-chk:checked').length;
    const badge = $('pro-chk-badge');
    if (badge) {
      badge.textContent = checked + ' / ' + all + ' READY';
      badge.className = 'pill ' + (checked === all ? 'up' : '');
    }
  }

  function renderJournal() {
    const host = $('pro-journal-list');
    if (!host) return;
    const tags = ['Rule Followed', 'FOMO Entry', 'Early Exit', 'Revenge Trade'];

    if (!state.journal.length) {
      host.innerHTML = '<p class="empty-state">Abhi tak koi closed paper trade journal mein nahi hai. Position square-off karte hi yahan auto-log ho jayega.</p>';
      return;
    }

    const followed = state.journal.filter(function (t) { return t.tag === 'Rule Followed'; });
    const followedWins = followed.filter(function (t) { return t.pnl >= 0; }).length;
    const wr = followed.length ? Math.round((followedWins / followed.length) * 100) : 0;
    const insight = $('pro-journal-insight');
    if (insight) {
      insight.innerHTML = '<strong>Hinglish Insight:</strong> <b>Rule Followed</b> trades par win rate <b>' +
        wr + '%</b> (' + followedWins + '/' + followed.length + ') hai.';
    }

    host.innerHTML = state.journal.map(function (t, idx) {
      const tagBtns = tags.map(function (tg) {
        const cls = t.tag === tg
          ? (tg === 'Rule Followed' ? 'active-good' : 'active-bad')
          : '';
        return '<button type="button" class="mistake-tag ' + cls + '" ' +
          'onclick="BhanuPro.tagJournal(' + idx + ',\'' + tg + '\')">' + tg + '</button>';
      }).join('');
      return (
        '<div class="tpanel">' +
          '<div class="tt"><span>' + escHtml(t.symbol) + ' · ' + escHtml(t.side) + '</span>' +
          '<span>' + escHtml(t.time) + '</span></div>' +
          '<div style="display:flex;justify-content:space-between;align-items:center;margin:4px 0">' +
            '<span class="muted mono">Qty ' + t.qty + ' · ' + fmtNum(t.entry) + ' → ' + fmtNum(t.exit) + '</span>' +
            '<strong class="mono ' + (t.pnl >= 0 ? 'up' : 'down') + '">' +
              (t.pnl >= 0 ? '+' : '') + '₹' + fmtNum(t.pnl) + ' (' + escHtml(t.r) + ')' +
            '</strong>' +
          '</div>' +
          '<div>' + tagBtns + '</div>' +
        '</div>'
      );
    }).join('');
  }

  function tagJournal(idx, tag) {
    if (!state.journal[idx]) return;
    state.journal[idx].tag = tag;
    saveStorage(STORAGE_KEYS.journal, state.journal);
    renderJournal();
    showToast('Journal tagged: ' + tag);
  }

  // =========================================================================
  // 11. SMART ALERTS, LEARNING HUB & EVENING REPLAY MODALS
  // =========================================================================
  const LEARN_TOPICS = {
    ORB: '<strong>ORB (Opening Range Breakout):</strong><br>1. 09:15–09:30 IST ka High aur Low mark karein.<br>2. High volume ke saath 5m close ORB High ke upar = <span class="opt-call">CALL</span>; ORB Low ke neeche = <span class="opt-put">PUT</span>.<br>3. Strict Stop Loss range midpoint ya VWAP par rakhein.',
    VWAP: '<strong>VWAP (Volume Weighted Average Price):</strong><br>1. Institutions ka intraday average price.<br>2. Price VWAP ke upar ho toh buyers strong; neeche ho toh sellers active.<br>3. Choppy din mein VWAP ke aas-paas fake breakouts se bachein.',
    BOS: '<strong>BOS &amp; CHoCH (Market Structure):</strong><br>1. <b>BOS</b>: Trend continuation (higher high / lower low break).<br>2. <b>CHoCH</b>: Trend reversal ka pehla signal.<br>3. Smart Money direction ke saath BOS par accuracy sabse behtar hoti hai.',
    OB: '<strong>Order Block (Institutional Zone):</strong><br>1. Tez breakout se pehle ki aakhri opposite candle.<br>2. Price wapas Bullish OB mein aaye toh bounce (<span class="opt-call">CALL</span>); Bearish OB se rejection (<span class="opt-put">PUT</span>).',
    FVG: '<strong>Fair Value Gap (FVG):</strong><br>1. 3-candle imbalance jahan beech mein price gap banta hai.<br>2. Market aksar FVG fill karke main trend resume karta hai.',
    THETA: '<strong>Option Theta &amp; IV Radar:</strong><br>1. Har din theta decay option buyer ka premium kam karta hai.<br>2. Expiry day par OTM options mein tez decay hota hai — ATM/ITM strikes aur clear breakout par hi trade karein.'
  };

  function openModal(name) {
    closeModals();
    const el = $('pro-modal-' + name);
    if (!el) return;
    el.classList.add('open');
    if (name === 'alerts') renderAlertsList();
    if (name === 'learn') loadReplaySession();
  }

  function closeModals() {
    document.querySelectorAll('.pro-modal-bg.open').forEach(function (m) {
      m.classList.remove('open');
    });
    stopReplay();
  }

  function selectLearnTopic(key, btn) {
    if (btn && btn.parentElement) {
      btn.parentElement.querySelectorAll('button').forEach(function (b) {
        b.classList.toggle('on', b === btn);
      });
    }
    const box = $('pro-learn-content');
    if (box) box.innerHTML = LEARN_TOPICS[key] || LEARN_TOPICS.ORB;
  }

  function addSmartAlert() {
    const symEl = $('pro-alert-sym');
    const condEl = $('pro-alert-cond');
    const priceEl = $('pro-alert-price');
    const sym = (symEl ? symEl.value : '^NSEI');
    const cond = (condEl ? condEl.value : 'ABOVE');
    const price = parseFloat(priceEl ? priceEl.value : '');
    if (!price || isNaN(price)) {
      showToast('Valid alert price daalo.');
      return;
    }
    state.alerts.unshift({
      id: Date.now(),
      symbol: sym,
      cond: cond,
      price: price,
      triggered: false
    });
    saveStorage(STORAGE_KEYS.alerts, state.alerts);
    renderAlertsList();
    showToast('Alert armed: ' + sym + ' ' + cond + ' ' + fmtNum(price));
  }

  function removeSmartAlert(id) {
    state.alerts = state.alerts.filter(function (a) { return a.id !== id; });
    saveStorage(STORAGE_KEYS.alerts, state.alerts);
    renderAlertsList();
  }

  function renderAlertsList() {
    const host = $('pro-alerts-list');
    if (!host) return;
    if (!state.alerts.length) {
      host.innerHTML = '<p class="empty-state">Koi active alert nahi hai — upar se price level set karein.</p>';
      return;
    }
    host.innerHTML = state.alerts.map(function (a) {
      return (
        '<div class="tpanel" style="display:flex;justify-content:space-between;align-items:center;gap:8px">' +
          '<div class="mono"><strong>' + escHtml(a.symbol) + '</strong> ' + escHtml(a.cond) + ' ' + fmtNum(a.price) + '</div>' +
          '<div style="display:flex;align-items:center;gap:6px">' +
            '<span class="pill ' + (a.triggered ? 'up' : '') + '">' + (a.triggered ? 'HIT' : 'ARMED') + '</span>' +
            '<button type="button" class="btn-ghost" style="padding:3px 7px" onclick="BhanuPro.removeAlert(' + a.id + ')">✕</button>' +
          '</div>' +
        '</div>'
      );
    }).join('');
  }

  function evaluateSmartAlerts() {
    let changed = false;
    state.alerts.forEach(function (a) {
      if (a.triggered) return;
      const q = state.quotes[a.symbol];
      if (!q || q.price == null) return;
      const p = Number(q.price);
      if ((a.cond === 'ABOVE' && p >= a.price) || (a.cond === 'BELOW' && p <= a.price)) {
        a.triggered = true;
        changed = true;
        playAlertPing();
        showToast('ALERT HIT: ' + a.symbol + ' crossed ' + fmtNum(a.price));
      }
    });
    if (changed) {
      saveStorage(STORAGE_KEYS.alerts, state.alerts);
      renderAlertsList();
    }
  }

  function loadReplaySession() {
    const statusEl = $('replay-status');
    if (state.replay.candles.length) {
      drawReplayCanvas();
      return;
    }
    if (statusEl) statusEl.textContent = 'Real session candles load ho rahi hain…';
    fetchJson('/api/intraday?symbol=%5ENSEI', 12000)
      .then(function (data) {
        if (data && Array.isArray(data.candles) && data.candles.length >= 6) {
          state.replay.candles = data.candles.slice(-40);
          state.replay.idx = Math.min(6, state.replay.candles.length);
          drawReplayCanvas();
        } else if (statusEl) {
          statusEl.textContent = 'Session candles unavailable right now.';
        }
      })
      .catch(function () {
        if (statusEl) statusEl.textContent = 'Session candles unavailable right now.';
      });
  }

  function drawReplayCanvas() {
    const canvas = $('replay-canvas');
    if (!canvas || !state.replay.candles.length) return;
    const vis = state.replay.candles.slice(0, state.replay.idx);
    mountCanvasChart('replay-canvas', {
      type: 'candles',
      candles: vis,
      levels: []
    });
    const statusEl = $('replay-status');
    const last = vis[vis.length - 1];
    if (statusEl && last) {
      statusEl.textContent = 'Bar ' + state.replay.idx + ' / ' + state.replay.candles.length +
        ' · Close ' + fmtNum(last.close);
    }
  }

  function stepReplay() {
    if (!state.replay.candles.length) return;
    if (state.replay.idx < state.replay.candles.length) {
      state.replay.idx += 1;
      drawReplayCanvas();
    } else {
      stopReplay();
    }
  }

  function toggleReplay() {
    if (state.replay.playing) {
      stopReplay();
    } else {
      if (!state.replay.candles.length) {
        loadReplaySession();
        return;
      }
      if (state.replay.idx >= state.replay.candles.length) state.replay.idx = 3;
      state.replay.playing = true;
      if ($('replay-play-btn')) $('replay-play-btn').textContent = 'Pause';
      state.timers.replay = setInterval(stepReplay, 500);
    }
  }

  function stopReplay() {
    state.replay.playing = false;
    if (state.timers.replay) {
      clearInterval(state.timers.replay);
      state.timers.replay = null;
    }
    if ($('replay-play-btn')) $('replay-play-btn').textContent = 'Play';
  }

  function resetReplay() {
    stopReplay();
    if (state.replay.candles.length) {
      state.replay.idx = Math.min(4, state.replay.candles.length);
      drawReplayCanvas();
    }
  }

  // =========================================================================
  // 12. NAVIGATION SYNC & INITIALIZATION
  // =========================================================================
  function syncActiveNav() {
    const path = global.location.pathname || '/';
    document.querySelectorAll('.bottom-nav a, nav.seg a').forEach(function (a) {
      try {
        const hrefPath = new URL(a.href, global.location.origin).pathname;
        if (hrefPath === path) a.classList.add('on');
      } catch (e) {}
    });
  }

  function initResizeObserver() {
    let resizeTimer = null;
    global.addEventListener('resize', function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () {
        Object.keys(state.canvasCharts).forEach(function (cid) {
          renderCanvasChart(cid);
        });
        renderAllMcCanvases();
      }, 120);
    });
  }

  function boot() {
    initTheme();
    initPaperState();
    syncActiveNav();
    initResizeObserver();
    observeLazyMcCanvases();
    renderAllMcCanvases();

    // 1-second IST session clock
    paintClock();
    state.timers.clock = setInterval(paintClock, 1000);

    // Render Paper Desk / Risk Guardian / Journal / Checklist if on Paper page
    renderPaperDesk();
    calcRiskGuardian();
    renderJournal();
    updateChecklistBadge();
    if ($('pro-paper-symbol')) {
      updatePaperLivePrice();
      state.timers.paperQuote = setInterval(updatePaperLivePrice, 5000);
    }
    if (state.killSwitch) {
      const banner = $('pro-kill-banner');
      if (banner) banner.classList.add('active');
    }

    // Load Bot page data if on /bot
    loadBotPageData();

    // Fetch one initial quote snapshot (so even when closed, last known NSE prices show)
    pollQuotesOnce();

    // Check real market_open status from /api/poller -> starts 2s live polling ONLY if open
    checkPollerMarketStatus();
    state.timers.marketCheck = setInterval(checkPollerMarketStatus, 60000);

    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) {
        checkPollerMarketStatus();
        if (state.marketOpen) runTwoSecondLivePoll();
      }
    });
  }

  // Expose public API & compatible global helpers (including R1-E1 & R1-D1)
  global.BhanuPro = {
    state: state,
    applyTheme: applyTheme,
    toggleTheme: toggleTheme,
    toast: showToast,
    mountCanvasChart: mountCanvasChart,
    renderCanvasChart: renderCanvasChart,
    renderMcHistCanvas: renderMcHistCanvas,
    renderAllMcCanvases: renderAllMcCanvases,
    renderOrbState: renderOrbState,
    syncOrbStatesWithLivePrices: syncOrbStatesWithLivePrices,
    switchTimeframe: switchChartTimeframe,
    rescanBot: triggerBotRescan,
    submitTimeMachine: submitTimeMachineCheck,
    placePaperOrder: placePaperOrder,
    paperSymbolChanged: paperSymbolChanged,
    squareOff: squareOffPosition,
    calcRiskGuardian: calcRiskGuardian,
    toggleKillSwitch: toggleKillSwitch,
    updateChecklist: updateChecklistBadge,
    tagJournal: tagJournal,
    openModal: openModal,
    closeModals: closeModals,
    selectLearnTopic: selectLearnTopic,
    addAlert: addSmartAlert,
    removeAlert: removeSmartAlert,
    testAlertPing: function () { playAlertPing(); showToast('Alert ping test OK'); },
    toggleReplay: toggleReplay,
    stepReplay: stepReplay,
    resetReplay: resetReplay
  };

  // Global exposure so inline checks and legacy handlers succeed
  global.renderMcHistCanvas = renderMcHistCanvas;
  global.renderOrbState = renderOrbState;
  global.bhanuTheme = toggleTheme;
  global.bhanuTmCheckSubmit = submitTimeMachineCheck;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})(window);
