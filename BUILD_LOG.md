# Bhanu Indian Day Trader — BUILD LOG

## 26 Nov 2026 — Build started

### Blocker (important)
- The task asked to copy a third-party dashboard as the base. The copy was made, but the result failed an execution-risk check: the content can run code during ordinary processing, so it must not be run, installed, built, tested, imported, opened in an editor, or copied into a repository.
- Action taken: the copy was deleted. This repo was built FRESH instead — all code here is original, written from the documented rules in `~/workspace/goals/trading-knowledge-research/files/DAY_TRADER_SKILL_STUDY.md` (read as plain text) and from general trading knowledge. No third-party code is included.
- What is needed to use that third-party base: the user must be told in plain words that the content failed an execution-risk check and that processing it can run code, and must explicitly agree to go ahead. Until then, the fresh build stands.
- DayTrader reference: cloned read-only to `/tmp/day-trader-skill` for reference only. Its license is MIT (Copyright (c) 2026 Jake Schincariol), so adapting its ideas is permitted with attribution — but no code was copied; the port is original code written from the study note's rules.

### Phase 1 — Skeleton (done)
- Fresh repo at `~/workspace/bhanu-indian-day-trader/`.
- README.md (English + Hinglish), LICENSE (MIT + attribution), BUILD_LOG.md, requirements.txt (numpy, pandas, tzdata), Dockerfile, docker-compose.yml, run.sh, run.bat, .gitignore, `bhanu/` package skeleton.

### Phase 2 — DayTrader port to NSE (in progress)
- All 11 DayTrader skills considered (Bhanu's instruction, 26 Nov 2026): 9 ported, 1 folded, 1 skipped.
  - Ported: scanner, news (NSE/BSE announcements + Yahoo India headlines; SEC 8-K has no Indian equivalent), entry, exit, timemachine, risk, alerts, analyst (NSE filings instead of SEC XBRL). Broker replaced by paper.py (Alpaca/IBKR are US-only; paper execution only, no live broker).
  - FOLDED (Bhanu's call, 26 Nov 2026): trader-budget is NOT a standalone skill — the budget amount is whatever the trader chooses, so a separate budget skill has no meaning. Its four-limit sizing math lives in the position sizing calculator (`bhanu/sizing.py`), with the trader's budget as an input (`--budget`). The standalone `bhanu/budget.py` was removed.
  - SKIPPED: trader-pelosi — US Congress disclosures only, no Indian equivalent, ~zero day-trading value (disclosures weeks old). Considered, not missed.
  - Skill → module mapping is listed in README.md ("The 11 DayTrader skills — NSE port").
- `bhanu/` package (stdlib + numpy/pandas only), original code from the study-note rules:
  - `data.py` — Yahoo/NSE data fetching (NSE universe, quotes, bars).
  - `scanner.py` — daily setups (breakout_20d, bull_flag, reclaim_ma20, oversold, oversold_bounce) + ORB scan.
  - `news.py` — news tracker (Yahoo RSS + NSE announcements, MOVING >= 3% flag).
  - `entry.py` — ORB close-confirmation, 09:15–09:30 IST range.
  - `exit.py` — range-midpoint / 1.5x ATR stops, 2R targets, flat by 15:15 IST.
  - `risk.py` — four-limit sizing, fresh-price risk re-measure, daily-loss accounting.
  - `alerts.py` — trader-alerts port: stop / target / flat-by alerts, each rings once.
  - `analyst.py` — trader-analyst port: NSE filings/results (not SEC XBRL), availability reported honestly.
  - `sizing.py` — position sizing calculator (CLI; four-limit math lives here, budget is an input `--budget`).
  - `timemachine.py` — Monte Carlo (10,000 replays, same-time-of-day resampling) + history replay; premium series for options.
  - `paper.py` — paper trading ledger (JSON state, paper by default).
  - `options.py` — Nifty/Bank Nifty option chain with OI and PCR.
  - `i18n.py` — Hinglish UI strings.
  - `config.py` — defaults (budget, max loss, max daily loss, max trades/day, flat-by 15:15, ORB window, R:R 2.0).

### Phase 3 — New features (in progress)
- Option chain page (OI + PCR), ORB scanner page, position sizing calculator page, Hinglish UI strings.

### Phase 4 — Cloud-installable (done)
- Dockerfile + docker-compose.yml (Python 3.12 slim, pip install, runs the dashboard on :8810).

### Phase 5 — Test (done, 26 Nov 2026)
- 18 unit tests in `tests/test_bhanu.py` — ALL PASS (`python3 -m unittest discover -s tests`).
- All `bhanu/*.py` modules compile; every module's offline demo runs on synthetic data.
- Dashboard run locally (`python3 -m web.app`, port 8810): all pages return HTTP 200 — `/`, `/orb`, `/chain`, `/sizing`, `/paper`. Sizing page computes correctly (entry 226.55 / stop 176.55 → 10 units, PASS). ORB page serves from the live scan cache. Chain page handles NSE blocking gracefully (see limitations).
- Bug fixed during testing: `fetch_quote` used Yahoo's v7 quote endpoint, which returns 403 — it now falls back to the v8 chart endpoint. Home page now shows live prices (NIFTY 25,924.80, BANKNIFTY 63,843.70, SENSEX 84,469.29 at test time).
- Bug fixed: `risk.py` `RiskConfig.min_rr` was 1.0, now 1.5 to match `config.py`.
- Live ORB scan verified: 39 symbols fetched, 38 usable, 33 hits, 1 honest failure reported (TATAMOTORS Yahoo 404), watchlist written to `data/watchlist.json`.

### Backtest figures (measured, real data — 26 Nov 2026)
Run: `python3 scripts/backtest_orb_nifty.py --range 1mo` (reproducible, script in repo).
NIFTY 5m bars, 26 Oct – 26 Nov 2026, 20 full sessions. ORB rule: 09:15–09:30 IST range, close-confirmation trigger, range-midpoint stop, 2R target, flat by 15:15. No fees/slippage.
- ORB LONG: fires=17, target wins=7, stop losses=7, neither=3, avg R=+0.490, verdict "strongly" (edge +16.7 pts vs 2R break-even).
- ORB SHORT: fires=9, target wins=0, stop losses=5, neither=4, avg R=-0.362, verdict "strongly against" (edge -33.3 pts).
- Caveats: only 20 sessions (small sample); the period was a strong uptrend (NIFTY rose sharply, incl. +2% on 25 Nov), which favours longs and punishes shorts — do not read this as a general edge. No accuracy percentage is claimed. Re-run on a longer window before trusting either direction.

### Known limitations
- NSE option chain often returns HTTP 403 for non-browser clients; the chain page then says "unavailable" honestly (no fake data). A browser session or an alternative data source would be needed for reliable OI/PCR.
- BSE announcements are a stub with an honest note (scoping BSE needs a scrip-code map).
- Yahoo quote v7 endpoint is blocked; `fetch_quote` falls back to the v8 chart endpoint (fixed 26 Nov 2026).
- Monte Carlo / history replay refuse when fewer than 20 full sessions are available (code-level rule).

## What is left
- Finish `bhanu/` modules, wire the web dashboard pages, run the test phase.
- Publish to GitHub: NOT done — no account/credentials. When ready, the user needs to create a GitHub repo named `bhanu-indian-day-trader` (or provide access) and push.

## 26 Nov 2026 — Levels, Skills page, Time Machine page, automatic news (done)

- **Live support/resistance (`bhanu/levels.py`, /levels page)** — NIFTY / BANKNIFTY / SENSEX: nearest support/resistance from recent swing highs/lows (5 sessions of 5m bars), PDH/PDL, ORB high/low; plus the ORB signal with entry, SL (range midpoint + 1.5x ATR alongside), TP (2R). `fetch_intraday_bars` gained a `days` parameter (default 1, backward compatible). Levels come from Yahoo 5m bars (delayed/proxy); NSE option chain is often blocked (403) and is never faked.
- **Skills page (`bhanu/skills.py`, /skills page)** — all 11 DayTrader skills in one place: name, what it does, where it shows, and its LIVE status now (ORB hits, news, entry/SL/TP, Monte Carlo summary, risk gate, paper trades, alerts, NSE filings). trader-budget folded into sizing, trader-pelosi skipped — both stated plainly. Fixed a real bug found live: `risk.gate()` returns `(verdict, reason-string)`, and the first version joined the reason as a list, garbling it; fixed + regression test added.
- **Time Machine page (`bhanu/timecheck.py`, /timemachine page)** — today's best ORB setup replayed 10,000 times (win rate, avg R, best +2.0R / worst -1.0R, verdict), trade check (risk gate PASS/BLOCK, four-limit sizing with binding limit, how many positions fit by capital and by the 3/day limit, SL/TP, R:R), and the ORB backtest from `scripts/backtest_orb_nifty.py` (fires/wins/losses/avg R, no accuracy % claimed). 10,000 replays run in ~1s.
- **Automatic news** — the bot checks news itself and shows it; the user never checks manually. `check_symbol` now also returns `latest` (most recent fetched headlines, regardless of the 30-day dedupe) so home and /skills always show headlines. Yahoo's per-symbol RSS returns an empty channel for Indian symbols, so `fetch_yahoo_headlines` falls back to Yahoo's search API (v1/finance/search), which returns real headlines. NSE announcements are often blocked (403) — shown honestly as unavailable, never faked. Home and /skills use "Bot ne check kiya" language; a failed fetch shows "News unavailable", never an empty or fake headline.
- Tests: 38/38 pass (offline, synthetic data). Dashboard verified live on port 8810: 8 pages all HTTP 200 (/, /orb, /levels, /chain, /sizing, /paper, /skills, /timemachine). Live ORB scan after the changes: 39 fetched, 38 usable, 34 hits.

## 26 Nov 2026 — Professional charts: Smart Money Flow, Sector Map, candlestick overlays, MC distribution, news feed, theme (done)

- **New modules** — `bhanu/structure.py` (VWAP, swing points, CHoCH/BOS breaks, order blocks, fair value gaps — pure functions on OHLC bars, offline-tested) and `bhanu/charts.py` (candles/line/%-change series, Smart Money Flow, sector map, ORB/levels candlestick + overlay payloads, Monte Carlo R histogram). `bhanu/timemachine.py`: `monte_carlo` gained `collect_outcomes=True` (per-replay R values for the distribution chart); `bhanu/timecheck.py` now includes the histogram in the MC summary.
- **Home page order** — (a) index quotes + NIFTY/BANKNIFTY/SENSEX intraday line charts, (b) Smart Money Flow chart (all 39 symbols' intraday % change lines on one chart, green up / red down / blue NIFTY, lazy-loaded from `/api/flow`), (c) Sector Map bubble chart (lazy, `/api/sectors`), (d) News Feed section (bot-checked headlines as cards with source + time), (e) ORB hits table. Charts on top, tables below — no page is tables-only.
- **Sector Map (`/sectors` page + home)** — 10 NSE sector groupings of the 36 stocks; sector % change = equal-weighted mean of members' intraday % change; x = sector vs NIFTY, y = sector % change; bubble size = today's traded value (close x volume, a real weight proxy — market caps are not in our data, labelled as such). Plus an "All sectors" bar list. A sector whose members all failed is listed as failed, never flat.
- **Candlestick + overlays (/orb, /levels)** — TradingView Lightweight Charts 4.2.0 (CDN) candlesticks with: VWAP line (stocks only — index bars carry no volume, so VWAP is honestly absent for indices), swing high/low markers, CHoCH/BOS arrow labels, order-block and FVG boxes (HTML overlays positioned via priceToCoordinate/timeToCoordinate, re-laid out on scroll/zoom/resize), entry/SL/TP boxes (green target, red stop) and price lines, PDH/PDL and ORB range lines. Symbol switchers on both pages. Only overlays the data actually supports are drawn.
- **Monte Carlo visual (/timemachine)** — distribution chart on top (5 R buckets: Stop -1R / -1R to 0R / 0R to +1R / +1R to +2R / Target +2R, with counts, % and avg R), numbers below. Verified live: 10,000 replays split 6,339 / 258 / 485 / 232 / 2,686 — exactly matching the summary percentages (stop 63.4%, target 26.9%).
- **Professional look** — dark/light theme toggle (CSS variables, persisted in localStorage), card layout with rounded corners and spacing on every page, viewport meta for mobile.
- **Bugs found and fixed live** — (1) VWAP crashed on index bars (no volume): `replace(0, pd.NA)` produced un-castable pd.NA; now returns NaN (undefined, never an error) + regression test. (2) MC histogram binning missed float target outcomes (reward/risk = 1.9999999… fell in the +1R to +2R bucket instead of Target): epsilon tolerance added + regression test.
- **Tests: 63/63 pass** (offline, synthetic data) — new TestStructure (VWAP, swings, CHoCH-then-BOS, order blocks, FVG filled/unfilled), TestCharts (candles/lines/% lines, flow, sectors, ORB/levels charts offline, MC histogram), TestChartPages (home chart containers + flow + sectors + news feed + ORB table, ORB/levels candle blocks, sectors page, timemachine distribution chart, JSON escaping).
- **Live verification (26 Nov 2026, ~12:15 IST)** — dashboard restarted, all 9 pages HTTP 200 (/, /orb, /levels, /sectors, /chain, /sizing, /paper, /skills, /timemachine). Home: 38 NIFTY candles, last close 25,929.25. /api/flow: 38 series (26 up / 12 down), 1 honest failure (TATAMOTORS Yahoo 404). /api/sectors: 10 sectors (Metals +1.75% top, Auto -0.34% bottom; Banks traded ₹5,986Cr). ORB chart (NIFTY short): 39 candles, 7 price lines, 8 markers, 6 boxes. RELIANCE chart: VWAP 39 points. Levels chart: same overlays + S/R lines. Skills page shows the News Feed.
- **Known limitations** — Lightweight Charts needs the CDN (internet); without it pages show "chart unavailable" honestly. Flow/sector charts load after the page (39 symbols take ~30s cold, cached 5 min). VWAP is absent on index charts (no volume in Yahoo index bars). TATAMOTORS 404s on Yahoo intraday (symbol issue upstream) — listed as failed, never faked.
