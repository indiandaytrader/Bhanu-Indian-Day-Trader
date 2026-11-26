# Bhanu Indian Day Trader 🇮🇳

**Free open-source NSE day-trading dashboard — made for Indian traders, in Hinglish.**

DayTrader (US) batata hai America ke market me kab ghusna hai aur kab nikalna hai. **Bhanu Indian Day Trader wahi kaam Indian market ke liye karta hai:** Nifty 50, Bank Nifty aur Sensex par ORB scanner, option chain (OI/PCR), entry/exit signals, position sizing aur Monte Carlo check — sab ek dashboard me.

**Why install it?**

- **ORB Scanner** — subah 09:15–09:30 IST ki opening range ka breakout scan; trigger sirf completed candle CLOSE par, wick par nahi
- **Option Chain** — Nifty / Bank Nifty ki option chain, OI, PCR aur max pain ke saath
- **Entry / Exit signals** — range-midpoint ya 1.5x ATR stop, 2R target, 15:15 IST tak flat
- **Position Sizing Calculator** — chaar limits se size nikalta hai; stop door ho to NO trade
- **Monte Carlo Time Machine** — trade ko 10,000 baar replay karke honest odds batata hai (backtest, guarantee nahi)
- **News Tracker** — bot khud NSE/BSE announcements aur India ki taaza headlines check karke dashboard par dikhata hai; tumko manually news dekhne ki zaroorat nahi — news ke baad hi trade karo. Optional: free Marketaux key (`MARKETAUX_API_KEY`, marketaux.com se signup karke) se extra global news + har article ka sentiment score (-1 se +1) bhi aata hai
- **Live Support / Resistance** — Nifty / Bank Nifty / Sensex ke live support-resistance levels, PDH/PDL aur ORB range ke saath — kahan se entry, kahan se exit, sab ek jagah
- **Hinglish interface** — Hindi me samjho, English me trade karo
- **Paper trading by default** — pehle paper par seekho; is repo se koi real order kabhi nahi lagta

Paper trading by default. Nothing here places a real order.

> **Educational software, not investment advice.** Patterns, backtests and signals are computed from public data by fixed rules and may be wrong or delayed. Past results do not predict future returns. You are responsible for every trade you take.

## Bhanu Indian Day Trader kya hai? (Hinglish me)

Ye ek free, open-source dashboard hai Indian market ke liye. Isme hai:

- **ORB Scanner** — subah 09:15–09:30 IST ki opening range ka breakout scan (sirf completed candle CLOSE dekhta hai, wick nahi)
- **Option Chain** — Nifty / Bank Nifty ki option chain, OI aur PCR ke saath
- **Entry / Exit rules** — range midpoint ya 1.5x ATR stop, 2R target, 15:15 IST tak flat
- **Position Sizing Calculator** — chaar limits se size nikalta hai, aur stop door ho to mana kar deta hai
- **Monte Carlo Time Machine** — trade ko 10,000 baar replay karke honest odds batata hai (backtest, guarantee nahi)
- **News Tracker** — bot khud NSE/BSE announcements aur India ki taaza headlines check karke dashboard par dikhata hai; tumko manually news dekhne ki zaroorat nahi — news ke baad hi trade karo. Optional: free Marketaux key (`MARKETAUX_API_KEY`, marketaux.com se signup karke) se extra global news + har article ka sentiment score (-1 se +1) bhi aata hai
- **Paper Trading** — pehle paper par, real money kabhi nahi (is repo se koi real order nahi lagta)

## DayTrader se kya alag hai?

This repo adapts [DayTrader](https://github.com/JakesChincariol/day-trader-skill) (11 Claude skills for US day trading) for the Indian market — NSE (IST clock, 09:15 open, 15:15 flat, Nifty/Bank Nifty/Sensex only, CE/PE options), plus an option chain with OI/PCR, live support/resistance levels, and a Hinglish interface. All code here is written fresh for NSE; no DayTrader code is copied.

## The 11 DayTrader skills — NSE port

Every one of DayTrader's 11 skills was considered. 9 are ported, 1 is folded into the position sizing calculator, 1 is skipped on purpose:

| DayTrader skill | Bhanu Indian Day Trader | Status |
|---|---|---|
| trader-scanner | `bhanu/scanner.py` | Ported — NSE universe (Nifty 50 / Bank Nifty / Sensex + F&O stocks), daily setups + ORB scan |
| trader-news | `bhanu/news.py` | Ported — NSE/BSE exchange announcements + Yahoo India headlines (SEC 8-K has no Indian equivalent) |
| trader-entry | `bhanu/entry.py` | Ported — ORB close-confirmation, 09:15–09:30 IST range |
| trader-exit | `bhanu/exit.py` | Ported — range-midpoint / 1.5x ATR stops, 2R targets, flat by 15:15 IST |
| trader-timemachine | `bhanu/timemachine.py` | Ported — 10,000 Monte Carlo replays + history replay (premium series for options, never spot) |
| trader-budget | `bhanu/sizing.py` | Folded in — no standalone skill: the four-limit sizing math (max loss / risk, position cap, max order, cash buffer) lives in the position sizing calculator, and the trader's budget is just an input to it (Bhanu's call, 26 Nov 2026) |
| trader-risk | `bhanu/risk.py` | Ported — PASS/BLOCK gate, fresh-price risk re-measure, daily-loss accounting |
| trader-broker | `bhanu/paper.py` | Replaced — Alpaca/IBKR are US-only; paper-trading execution only, no live broker |
| trader-alerts | `bhanu/alerts.py` | Ported — stop / target / flat-by alerts, each rings once |
| trader-analyst | `bhanu/analyst.py` | Ported — NSE filings/results instead of SEC XBRL (data availability reported honestly) |
| trader-pelosi | — | **SKIPPED on purpose** — US Congress disclosures only, no Indian equivalent, and ~zero day-trading value (disclosures are weeks old). Considered, not missed. |

## Install (local)

You need Python 3.10 or newer.

```bash
git clone <this repo>
cd bhanu-indian-day-trader
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
bash run.sh
```

Then open http://localhost:8810. Keep the window running while you use it.

Windows:

```bat
run.bat
```

## Install (Docker / cloud)

```bash
docker compose up --build
```

Then open http://localhost:8810.

## Where it runs (kahan chalega?)

- **Apne PC par (Windows / Mac / Linux)** — Python install karo, `run.bat` (Windows) ya `bash run.sh` chalao, aur browser me http://localhost:8810 kholo. Jab tak window khuli hai, dashboard chalta hai.
- **Docker / cloud par** — `docker compose up --build` se kisi bhi server (VPS, Render, Railway) par chalao. Ek baar cloud par chal gaya to link kahi se bhi khul jayega — 24x7.
- **Mobile par** — dashboard ek web page hai. Cloud par host karo (upar wala tareeka), phir phone ke browser me wahi link kholo — mobile par bhi chalega. Phone par seedha install karna (Termux/Android) mushkil hai; cloud link hi asaan rasta hai.

## Use it (CLI)

```bash
.venv/bin/python -m bhanu.scanner daily      # pre-market daily setups
.venv/bin/python -m bhanu.scanner orb        # intraday ORB scan (after 09:30 IST)
.venv/bin/python -m bhanu.options chain      # Nifty option chain with OI + PCR
.venv/bin/python -m bhanu.sizing --help      # position sizing calculator
.venv/bin/python -m bhanu.timemachine --help # Monte Carlo + history replay
.venv/bin/python -m bhanu.levels             # live support/resistance + ORB entry/exit
.venv/bin/python -m bhanu.skills             # all 11 skills — live status
.venv/bin/python -m bhanu.timecheck          # time machine + trade check + backtest
.venv/bin/python -m bhanu.charts             # chart data (candles, flow, sectors)
.venv/bin/python -m bhanu.structure          # VWAP, swings, CHoCH/BOS, order blocks, FVG
```

## Dashboard pages

Charts upar, tables neeche — koi page sirf tables ka nahi. Charts TradingView Lightweight Charts se bante hain — library repo me hi bundled hai (`web/static/lightweight-charts.js`), CDN par depend nahi karti; library na load ho to built-in SVG chart dikhta hai, khaali box kabhi nahi.

**Real-time, not refresh:** quotes aur home ke index charts **har 5 second** me tick karte hain — background engine 3 indices ko har 5 second spark karta hai aur browser chart ka aakhri point **jagah par** update karta hai (`/api/live`), line aage badhti dikhti hai; page reload kabhi nahi, 60 second ka wait kabhi nahi. Upar **"● LIVE — last update HH:MM:SS"** indicator har successful tick par update hota hai. Har page turant khulta hai (koi server fetch page ko block nahi karta — quotes, charts, scans, news, Time Machine sab `/api/*` se page ke baad load hote hain, "loading" state ke saath). Dheere badalne wale sections (flow, sectors, news, ORB) apni natural cadence (60 second / 2–5 minute) par jagah par sync hote hain. Fetch fail ho to purana data dikhta rahta hai aur card me "retry ho raha hai" dikhta hai — khaali ya fake data kabhi nahi. Live prices NSE API se aate hain, taaki delay na ho; jo data delayed ho use honestly dikhaya jata hai, fake live nahi banaya jata.

**Live engine (background poller):** server chalu hote hi background threads market data ko garam rakhte hain — NSE indices har 15 second, Yahoo batch feed (20 symbols ek call me, 1-minute closes): 3 indices har **5 second** (live quote feed), poore 39 symbols har 60 second (flow/sector input), index intraday bars har 60 second, Smart Money Flow har 5 minute, news check har 5 minute, ORB/levels scan har 2 minute, Time Machine har 10 minute. Page khulte waqt koi fetch nahi hota — data pehle se memory me hota hai. Fetch fail ho to aakhri sahi value dikhti hai; rate limit (429) aaye to 120 second ka backoff lagta hai. Aaj ka intraday data har 5 minute disk par snapshot hota hai, taaki restart ke baad bhi aaj ka asli data dikhe. Robot (entry/exit engine) hamesha isi fresh data + fresh news par signal banata hai — stale entry kabhi nahi.

- **/ — Home** — (a) index quotes + NIFTY / BANKNIFTY / SENSEX intraday line charts, (b) Smart Money Flow chart — saare 39 symbols ki intraday % change lines ek hi chart par (green = up, red = down, blue = NIFTY), (c) Sector Map bubble chart, (d) News Feed — bot khud check karke dikhata hai (source + time ke saath), (e) ORB hits table, (f) **Robot — live engine** card: aakhri scan times, kitne symbols scan hue, ORB hits, news check time, Time Machine replays aur best setup (entry/SL/TP1/TP2/TP3), (g) Levels mini-summary — indices ke support/resistance + signal, (h) Time Machine mini-summary — aaj ka best setup + 10,000 replays ke odds.
- **/orb — ORB Scanner + candlestick chart** — top hit ka candlestick chart: VWAP line, swing highs/lows markers, CHoCH/BOS labels, order-block aur fair-value-gap boxes, entry/SL/TP boxes (green target, red stop), PDH/PDL aur ORB range lines. Symbol switcher se kisi bhi hit ka chart dekho.
- **/levels — Live Support / Resistance + candlestick chart** — wahi overlays, index ke live levels ke saath: nearest support/resistance (recent swing highs/lows, PDH/PDL aur ORB range se), ORB entry, SL (range midpoint + 1.5x ATR), TP (2R) aur signal — kahan se entry, kahan se exit, sab ek jagah. Levels Yahoo 5m bars se aate hain (public, delayed, proxy) — live platform par verify karo. NSE option chain aksar blocked rehta hai (HTTP 403); chain data kabhi fake nahi kiya jata.
- **/sectors — Sector Map** — har sector ek bubble: green = up, red = down, size = aaj ka traded value (close × volume), x = sector vs NIFTY, y = sector % change. Saath me "All sectors" bar list (sector + % change). Sector % change = members ki intraday % change ka equal-weighted mean.
- **/skills — All 11 skills, ek jagah** — har DayTrader skill: kya karta hai, kahan dikhta hai, aur uska LIVE status abhi (ORB hits, taaze news headlines, entry/SL/TP, Monte Carlo summary, risk gate, paper trades, alerts, NSE filings). trader-budget sizing me folded hai, trader-pelosi skipped hai — dono saaf likhe hain, chhupaye nahi. Neeche News Feed section bhi hai.
- **/timemachine — Time Machine + Trade Check + Backtest** — upar Monte Carlo distribution chart (10,000 replays kaise bate: target pehle / stop pehle / neither, avg R ke saath), neeche numbers: aaj ke best ORB setup ko 10,000 baar replay karke win rate / avg R / best case / worst case; usi setup par risk gate (PASS/BLOCK), chaar limits se position size, kitni positions ban sakti hain, SL/TP aur R:R; aur NIFTY ORB backtest (fires, wins, losses, avg R). Koi accuracy % claim nahi — Monte Carlo reality check hai, edge ka proof nahi.

Dark / light theme toggle upar right corner me hai.

## Rules (the short version)

- ORB range = 09:15–09:30 IST (first 15 minutes). Trigger = first completed 5m bar that CLOSES above the range high. A wick above is not a trigger.
- Stops: range midpoint, or 1.5x ATR. Targets: 2R. Flat by 15:15 IST. Never widen a stop once the trade is on.
- Sizing = smallest of four limits (max loss / risk, position cap / entry, max order / entry, cash / entry). If the stop is too far for the max loss, no trade.
- Risk is re-measured on a fresh price before sizing — a stale entry understates risk.
- Daily loss counts the FULL max loss of every entry placed today, win or lose.
- Paper trading only. No broker is connected. No real order is ever placed.

## Honest numbers

No accuracy percentage is claimed anywhere in this repo. The only figures below come from a real run of this repo's code on real data (`python3 scripts/backtest_orb_nifty.py --range 1mo`, 26 Nov 2026, NIFTY 5m bars, 26 Oct – 26 Nov 2026, 20 full sessions; ORB rule: 09:15–09:30 IST range, close-confirmation trigger, range-midpoint stop, 2R target, flat by 15:15; no fees/slippage):

- ORB LONG: 17 fires, 7 target wins, 7 stop losses, 3 neither, avg R +0.490.
- ORB SHORT: 9 fires, 0 target wins, 5 stop losses, 4 neither, avg R −0.362.

Caveats: only 20 sessions (small sample), and the period was a strong uptrend, which favours longs and punishes shorts. Do not read this as a general edge. A Monte Carlo replay is a reality check, not proof of an edge — it resamples past bars and cannot see the future.

## Credits

- DayTrader skill by Jake Schincariol (JakesChincariol/day-trader-skill, MIT) — the 11-skill pipeline, ORB close-confirmation, four-limit sizing, Monte Carlo time machine and risk-gate ideas, adapted here for NSE.
- MIT-licensed; see LICENSE.

## License

MIT. See [LICENSE](LICENSE).
