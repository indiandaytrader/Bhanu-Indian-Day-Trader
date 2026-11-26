"""Combined entry/exit signal: ORB + levels + news + Time Machine + volume.

News used to only show on the dashboard. Now it votes on every signal,
and so do the Time Machine replay and the trigger bar's volume:

* ORB — the trigger itself (first 5m close outside the 09:15-09:30
  range). No trigger = no signal at all.
* LEVELS — nearest support/resistance: a level sitting between the
  entry and TP1 means the move must clear it first (half size).
* NEWS — keyword sentiment on fresh headlines. Fresh news AGAINST the
  trade direction blocks the entry; fresh news FOR the direction
  supports it. News that cannot be fetched never blocks — the signal
  says so plainly ("news unavailable, price-only signal").
* TIME MACHINE — the 10,000-replay Monte Carlo target-first share.
  Below 25% blocks the entry; 25%-33.3% (below the 2R break-even)
  halves the size.
* VOLUME — the trigger bar's relative volume. WEAK (<0.8x the 20-bar
  average) halves the size; STRONG (>=1.5x) confirms the breakout.

Decision: BLOCKED if any factor blocks; HALF SIZE if any factor says
half and none blocks; otherwise ENTER. Every factor's verdict and
reason is carried on the signal, so the dashboard can show the full
breakdown.

Exit: fresh news against an OPEN position gives an exit signal —
"EXIT NOW" when the news is severe (two or more against-items, or a
legal/halt tag, or several negative keywords in one headline), else
"EXIT EARLY" (tighten the stop / book early). News is a filter and an
exit trigger, never an entry signal by itself.

No accuracy percentage is ever claimed. Paper trading only: nothing
here places an order.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

#: Monte Carlo target-first share below this blocks the entry.
MC_BLOCK_BELOW = 25.0
#: Break-even target-first share for a 2R target (1 / (1 + 2)).
MC_BREAKEVEN_2R = 100.0 / 3.0

#: Fresh = published today (IST) or within the last 24 hours. An item
#: whose date cannot be parsed is treated as fresh — its age cannot be
#: proven stale, and blocking on it would be a guess either way.
FRESH_WINDOW = timedelta(hours=24)


def opt_label(direction: str) -> str:
    """User-facing option label for a trade direction: CALL for a long,
    PUT for a short. Display only — the internal direction stays
    'long'/'short' everywhere (logic, API, sizing)."""
    return "PUT" if str(direction).lower() == "short" else "CALL"


# ---------------------------------------------------------------------------
# News sentiment (keyword scoring on headlines)
# ---------------------------------------------------------------------------

#: Words that make a headline negative for the company/index.
NEGATIVE_WORDS: tuple[str, ...] = (
    "fall", "falls", "fell", "crash", "crashes", "probe", "fraud",
    "loss", "losses", "downgrade", "scam", "ban", "penalty", "plunge",
    "slump", "raid", "default", "miss", "misses", "decline", "drop",
    "drops", "investigation", "fine", "sell-off", "weak",
)
#: Words that make a headline positive for the company/index.
POSITIVE_WORDS: tuple[str, ...] = (
    "surge", "surges", "profit", "profits", "record", "upgrade",
    "buyback", "dividend", "order win", "rise", "rises", "jump",
    "jumps", "growth", "beat", "beats", "strong", "gain", "gains",
    "all-time high", "contract win", "deal",
)


def _count_matches(text: str, words: tuple[str, ...]) -> list[str]:
    """Words from ``words`` found in ``text`` (word-boundary match)."""
    lowered = f" {text.lower()} "
    found: list[str] = []
    for word in words:
        pattern = r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])"
        if re.search(pattern, lowered):
            found.append(word)
    return found


def score_headline(title: str) -> dict[str, Any]:
    """Sentiment of one headline: negative / positive / neutral.

    Returns ``{"sentiment", "negative_words", "positive_words"}``.
    A headline with more negative than positive keywords is negative
    and vice versa; a tie (or none) is neutral.
    """
    neg = _count_matches(title, NEGATIVE_WORDS)
    pos = _count_matches(title, POSITIVE_WORDS)
    if len(neg) > len(pos):
        sentiment = "negative"
    elif len(pos) > len(neg):
        sentiment = "positive"
    else:
        sentiment = "neutral"
    return {
        "sentiment": sentiment,
        "negative_words": neg,
        "positive_words": pos,
    }


def _parse_published(text: str) -> datetime | None:
    """Parse a headline's published string to an aware datetime (UTC).

    Handles Yahoo search ("26 Nov 2026 08:30 UTC"), RSS pubDate
    (RFC 2822) and plain dates ("26-Nov-2026", ISO). Returns None when
    the date cannot be parsed.
    """
    if not text:
        return None
    text = str(text).strip()
    for fmt in ("%d %b %Y %H:%M UTC", "%d %b %Y %H:%M:%S UTC",
                "%d-%b-%Y", "%d %b %Y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=IST).astimezone(ZoneInfo("UTC"))
        except ValueError:
            continue
    try:
        dt = parsedate_to_datetime(text)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)
            return dt.astimezone(ZoneInfo("UTC"))
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt.astimezone(ZoneInfo("UTC"))
    except ValueError:
        return None


def _is_fresh(item: dict[str, Any], now: datetime | None = None) -> bool:
    """True when the item was published today (IST) or in the last 24h.

    An unparseable date counts as fresh — its age cannot be proven.
    """
    now = now or datetime.now(ZoneInfo("UTC"))
    published = _parse_published(item.get("published", ""))
    if published is None:
        return True
    if published.date() == now.astimezone(IST).date():
        return True
    return now - published <= FRESH_WINDOW


def _fresh_items(news_result: dict[str, Any]) -> list[dict[str, Any]]:
    """The fresh items from a news check (``latest`` first, else ``items``)."""
    items = news_result.get("latest") or news_result.get("items") or []
    return [it for it in items if _is_fresh(it)]


# ---------------------------------------------------------------------------
# Factors
# ---------------------------------------------------------------------------

def news_factor(
    symbol: str, direction: str, news_result: dict[str, Any] | None = None
) -> dict[str, Any]:
    """News verdict for a planned ``direction`` trade in ``symbol``.

    ``news_result`` is a ``bhanu.news.check_symbol`` result; when None
    it is fetched live. Direction-aware: for a long, negative news is
    against and positive news is for; for a short it is the reverse.
    Returns ``{"verdict": "block"|"support"|"neutral", "reason",
    "available", "against": [...], "for": [...], "headline", "source",
    "published"}``.
    """
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")
    if news_result is None:
        try:
            from bhanu import news as news_mod

            news_result = news_mod.check_symbol(symbol)
        except Exception as exc:  # noqa: BLE001 - news never blocks on failure
            return {
                "available": False,
                "verdict": "neutral",
                "reason": "News unavailable — price-only signal.",
                "against": [],
                "for": [],
                "headline": "",
                "source": "",
                "published": "",
                "error": str(exc),
            }
    if news_result.get("unavailable"):
        # Caller already tried to fetch and failed (e.g. cached fetch
        # on the dashboard) — same neutral verdict, no second fetch.
        return {
            "available": False,
            "verdict": "neutral",
            "reason": "News unavailable — price-only signal.",
            "against": [],
            "for": [],
            "headline": "",
            "source": "",
            "published": "",
        }
    fresh = _fresh_items(news_result)
    against: list[dict[str, Any]] = []
    supporting: list[dict[str, Any]] = []
    for item in fresh:
        scored = score_headline(item.get("title", ""))
        item = {**item, **scored}
        if scored["sentiment"] == "negative":
            (against if direction == "long" else supporting).append(item)
        elif scored["sentiment"] == "positive":
            (supporting if direction == "long" else against).append(item)
    top = against[0] if against else (supporting[0] if supporting else None)
    if against:
        return {
            "available": True,
            "verdict": "block",
            "reason": (
                f"Fresh news against this {opt_label(direction)}: "
                f"'{against[0].get('title', '')}' "
                f"({against[0].get('source', '')}"
                + (f", {against[0].get('published', '')}" if against[0].get("published") else "")
                + f") — {len(against)} against item(s)."
            ),
            "against": against,
            "for": supporting,
            "headline": top.get("title", "") if top else "",
            "source": top.get("source", "") if top else "",
            "published": top.get("published", "") if top else "",
        }
    if supporting:
        return {
            "available": True,
            "verdict": "support",
            "reason": (
                f"Fresh news supports this {opt_label(direction)}: "
                f"'{supporting[0].get('title', '')}' "
                f"({supporting[0].get('source', '')}"
                + (f", {supporting[0].get('published', '')}" if supporting[0].get("published") else "")
                + ")."
            ),
            "against": against,
            "for": supporting,
            "headline": top.get("title", "") if top else "",
            "source": top.get("source", "") if top else "",
            "published": top.get("published", "") if top else "",
        }
    return {
        "available": True,
        "verdict": "neutral",
        "reason": "No fresh news for or against this trade.",
        "against": against,
        "for": supporting,
        "headline": "",
        "source": "",
        "published": "",
    }


def mc_factor(
    setup: dict[str, Any] | None, mc: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Time Machine verdict for ``setup`` (a levels row / ORB setup).

    ``mc`` is a ``bhanu.timecheck.monte_carlo_summary`` result; when
    None it is computed live (fetches 60 days of 5m bars and runs
    10,000 replays — slow; pass a cached result on the dashboard).
    Below 25% target-first blocks; 25% to break-even halves the size.
    """
    if mc is None:
        if not setup:
            return {
                "available": False,
                "verdict": "neutral",
                "reason": "Time Machine not computed for this symbol — price-only signal.",
                "target_first_pct": None,
            }
        try:
            from bhanu import timecheck as timecheck_mod

            mc = timecheck_mod.monte_carlo_summary(setup)
        except Exception as exc:  # noqa: BLE001 - MC never blocks on failure
            return {
                "available": False,
                "verdict": "neutral",
                "reason": f"Time Machine unavailable — {exc}.",
                "target_first_pct": None,
            }
    if not mc.get("available"):
        return {
            "available": False,
            "verdict": "neutral",
            "reason": f"Time Machine unavailable — {mc.get('reason', 'no data')}.",
            "target_first_pct": None,
        }
    pct = float(mc["target_first_pct"])
    n = mc.get("n", 0)
    break_even = float(mc.get("break_even_pct") or MC_BREAKEVEN_2R)
    if pct < MC_BLOCK_BELOW:
        return {
            "available": True,
            "verdict": "block",
            "reason": (
                f"Time Machine: target first only {pct:.1f}% in "
                f"{n:,} replays (below {MC_BLOCK_BELOW:.0f}%) — history "
                f"says this setup rarely reaches target first."
            ),
            "target_first_pct": pct,
            "stop_first_pct": mc.get("stop_first_pct"),
            "neither_pct": mc.get("neither_pct"),
            "avg_r": mc.get("avg_r"),
            "n": n,
        }
    if pct < break_even:
        return {
            "available": True,
            "verdict": "half",
            "reason": (
                f"Time Machine: target first {pct:.1f}% in {n:,} replays — "
                f"below the {break_even:.1f}% break-even for a 2R target, "
                f"half size."
            ),
            "target_first_pct": pct,
            "stop_first_pct": mc.get("stop_first_pct"),
            "neither_pct": mc.get("neither_pct"),
            "avg_r": mc.get("avg_r"),
            "n": n,
        }
    return {
        "available": True,
        "verdict": "allow",
        "reason": (
            f"Time Machine: target first {pct:.1f}% in {n:,} replays — "
            f"at/above the {break_even:.1f}% break-even for a 2R target."
        ),
        "target_first_pct": pct,
        "stop_first_pct": mc.get("stop_first_pct"),
        "neither_pct": mc.get("neither_pct"),
        "avg_r": mc.get("avg_r"),
        "n": n,
    }


def volume_factor(
    symbol: str,
    bars: Any = None,
    trigger_time: datetime | str | None = None,
    yahoo_symbol: str | None = None,
    volume_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Volume verdict for ``symbol`` at ``trigger_time``.

    ``volume_result`` is a ``bhanu.volume.analyse_volume`` result;
    when None it is computed live. STRONG supports the breakout,
    WEAK halves the size, normal is neutral.
    """
    if volume_result is None:
        from bhanu import volume as volume_mod

        volume_result = volume_mod.analyse_volume(
            symbol, bars=bars, trigger_time=trigger_time,
            yahoo_symbol=yahoo_symbol,
        )
    verdict = volume_result.get("verdict", "normal")
    rel = volume_result.get("rel_vol")
    base_reason = volume_result.get("reason", "")
    if rel is not None:
        rel_txt = f"{rel:.1f}x avg"
        if verdict == "STRONG":
            decision = "support"
            reason = f"Volume: {rel_txt} — STRONG. {base_reason}"
        elif verdict == "WEAK":
            decision = "half"
            reason = f"Volume: {rel_txt} — WEAK. {base_reason}"
        else:
            decision = "neutral"
            reason = f"Volume: {rel_txt} — normal. {base_reason}"
    else:
        decision = "neutral"
        reason = f"Volume: normal. {base_reason}"
    return {
        "verdict": decision,
        "volume_verdict": verdict,
        "rel_vol": rel,
        "trend": volume_result.get("trend"),
        "reason": reason,
    }


def levels_factor(
    direction: str,
    entry: float,
    stop: float,
    tp1: float,
    support: float | None,
    resistance: float | None,
) -> dict[str, Any]:
    """S/R verdict: a level between entry and TP1 must be cleared first.

    For a long, the nearest resistance between entry and TP1 halves
    the size; resistance already below the entry supports the trade.
    For a short it is the mirror image with support. No levels data
    is neutral, never a block.
    """
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")
    if direction == "long":
        if resistance is not None and entry < resistance < tp1:
            return {
                "verdict": "half",
                "reason": (
                    f"Nearest resistance {resistance:,.2f} sits between "
                    f"entry {entry:,.2f} and TP1 {tp1:,.2f} — the move "
                    f"must clear it first."
                ),
            }
        if resistance is not None and resistance <= entry:
            return {
                "verdict": "support",
                "reason": (
                    f"Price already above nearest resistance "
                    f"{resistance:,.2f}."
                ),
            }
        return {"verdict": "neutral",
                "reason": "No resistance between entry and TP1."}
    if support is not None and tp1 < support < entry:
        return {
            "verdict": "half",
            "reason": (
                f"Nearest support {support:,.2f} sits between entry "
                f"{entry:,.2f} and TP1 {tp1:,.2f} — the move must clear "
                f"it first."
            ),
        }
    if support is not None and support >= entry:
        return {
            "verdict": "support",
            "reason": f"Price already below nearest support {support:,.2f}.",
        }
    return {"verdict": "neutral",
            "reason": "No support between entry and TP1."}


# ---------------------------------------------------------------------------
# Combined entry signal
# ---------------------------------------------------------------------------

def entry_signal(
    symbol: str,
    direction: str,
    entry: float,
    stop: float,
    target: float,
    tp1: float | None = None,
    tp3: float | None = None,
    support: float | None = None,
    resistance: float | None = None,
    bars: Any = None,
    trigger_time: datetime | str | None = None,
    yahoo_symbol: str | None = None,
    news_result: dict[str, Any] | None = None,
    mc_result: dict[str, Any] | None = None,
    volume_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full entry signal for one ORB setup.

    ``news_result`` / ``mc_result`` / ``volume_result`` are pre-fetched
    factor inputs; any that is None is fetched live (news and volume
    are quick; the Monte Carlo is slow — pass the cached
    /timemachine result on the dashboard). Returns the decision
    (ENTER / HALF SIZE / BLOCKED), the size factor, entry/SL/TP1/TP2/TP3
    and every factor's verdict + reason.
    """
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")
    if not entry or not stop or not target:
        return {
            "symbol": symbol,
            "direction": direction,
            "decision": "NO SIGNAL",
            "size_factor": 0.0,
            "entry": entry,
            "stop": stop,
            "tp1": tp1,
            "tp2": target,
            "tp3": tp3,
            "factors": {},
            "reasons": ["No ORB setup — no entry, stop or target today."],
            "summary": f"{symbol} {opt_label(direction)}: no ORB signal today.",
        }
    risk = abs(float(entry) - float(stop))
    sign = 1.0 if direction == "long" else -1.0
    if tp1 is None:
        tp1 = round(float(entry) + sign * risk, 2)
    if tp3 is None:
        tp3 = round(float(entry) + sign * risk * 3.0, 2)

    setup = {
        "symbol": symbol,
        "signal": direction,
        "entry": float(entry),
        "stop": float(stop),
        "target": float(target),
    }
    factors = {
        "orb": {
            "verdict": "trigger",
            "reason": (
                f"ORB {opt_label(direction)}: first completed 5m bar closed "
                f"{'above' if direction == 'long' else 'below'} the "
                f"09:15–09:30 range at {float(entry):,.2f}."
            ),
        },
        "levels": levels_factor(
            direction, float(entry), float(stop), float(tp1),
            support, resistance,
        ),
        "news": news_factor(symbol, direction, news_result),
        "mc": mc_factor(setup, mc_result),
        "volume": volume_factor(
            symbol, bars=bars, trigger_time=trigger_time,
            yahoo_symbol=yahoo_symbol, volume_result=volume_result,
        ),
    }
    verdicts = [f["verdict"] for f in factors.values()]
    if "block" in verdicts:
        decision, size_factor = "BLOCKED", 0.0
    elif "half" in verdicts:
        decision, size_factor = "HALF SIZE", 0.5
    else:
        decision, size_factor = "ENTER", 1.0
    reasons = [f"{name}: {f['reason']}" for name, f in factors.items()]
    blocked = [name for name, f in factors.items() if f["verdict"] == "block"]
    halved = [name for name, f in factors.items() if f["verdict"] == "half"]
    if decision == "BLOCKED":
        summary = f"{symbol} {opt_label(direction)}: BLOCKED by {', '.join(blocked)}."
    elif decision == "HALF SIZE":
        summary = f"{symbol} {opt_label(direction)}: HALF SIZE — {', '.join(halved)}."
    else:
        summary = (
            f"{symbol} {opt_label(direction)}: ENTER @ {float(entry):,.2f}, "
            f"SL {float(stop):,.2f}, TP1 {float(tp1):,.2f} / "
            f"TP2 {float(target):,.2f} / TP3 {float(tp3):,.2f}."
        )
    return {
        "symbol": symbol,
        "direction": direction,
        "decision": decision,
        "size_factor": size_factor,
        "entry": float(entry),
        "stop": float(stop),
        "tp1": float(tp1),
        "tp2": float(target),
        "tp3": float(tp3),
        "factors": factors,
        "reasons": reasons,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Exit signal (open position + fresh news)
# ---------------------------------------------------------------------------

def exit_signal(
    symbol: str,
    direction: str,
    entry: float,
    news_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Exit check for an OPEN ``direction`` position in ``symbol``.

    Fresh news against the position triggers an exit: "EXIT NOW" when
    severe (two or more against-items, or a legal/halt tag, or several
    negative keywords in one headline), else "EXIT EARLY" (tighten the
    stop / book early). No fresh news against the position = HOLD.
    ``news_result`` is a ``bhanu.news.check_symbol`` result; when None
    it is fetched live.
    """
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")
    if news_result is None:
        try:
            from bhanu import news as news_mod

            news_result = news_mod.check_symbol(symbol)
        except Exception as exc:  # noqa: BLE001 - news never forces an exit
            return {
                "symbol": symbol,
                "direction": direction,
                "action": "HOLD",
                "reason": "News unavailable — hold on price action (stop/target unchanged).",
                "headline": "",
                "source": "",
                "published": "",
                "error": str(exc),
            }
    if news_result.get("unavailable"):
        return {
            "symbol": symbol,
            "direction": direction,
            "action": "HOLD",
            "reason": "News unavailable — hold on price action (stop/target unchanged).",
            "headline": "",
            "source": "",
            "published": "",
        }
    fresh = _fresh_items(news_result)
    against: list[dict[str, Any]] = []
    for item in fresh:
        scored = score_headline(item.get("title", ""))
        item = {**item, **scored}
        bad = scored["sentiment"] == "negative" and direction == "long"
        bad = bad or (scored["sentiment"] == "positive" and direction == "short")
        if bad:
            against.append(item)
    if not against:
        return {
            "symbol": symbol,
            "direction": direction,
            "action": "HOLD",
            "reason": "No fresh news against this position.",
            "headline": "",
            "source": "",
            "published": "",
        }
    top = against[0]
    severe = (
        len(against) >= 2
        or len(top.get("negative_words", []) or []) >= 2
        or len(top.get("positive_words", []) or []) >= 2
        or bool(set(top.get("tags", []) or []) & {"legal", "halt"})
    )
    action = "EXIT NOW" if severe else "EXIT EARLY"
    return {
        "symbol": symbol,
        "direction": direction,
        "action": action,
        "reason": (
            f"{action} — fresh news against this {opt_label(direction)}: "
            f"'{top.get('title', '')}' ({top.get('source', '')}"
            + (f", {top.get('published', '')}" if top.get("published") else "")
            + f") — {len(against)} against item(s)."
        ),
        "headline": top.get("title", ""),
        "source": top.get("source", ""),
        "published": top.get("published", ""),
    }


def main() -> int:
    # Offline demo with injected factor inputs (no network).
    news_ok = {
        "latest": [{
            "source": "Yahoo headline",
            "title": "Reliance Q2 profit rises 12% to a record",
            "link": "", "published": "", "tags": ["earnings"],
        }]
    }
    news_bad = {
        "latest": [{
            "source": "Yahoo headline",
            "title": "SEBI probe: company faces fraud penalty, shares fall",
            "link": "", "published": "", "tags": ["legal"],
        }]
    }
    mc_good = {"available": True, "target_first_pct": 40.0,
               "stop_first_pct": 50.0, "neither_pct": 10.0, "avg_r": 0.2,
               "n": 10000, "break_even_pct": 33.3}
    mc_bad = {"available": True, "target_first_pct": 18.0,
              "stop_first_pct": 70.0, "neither_pct": 12.0, "avg_r": -0.4,
              "n": 10000, "break_even_pct": 33.3}
    vol_strong = {"symbol": "NIFTY", "rel_vol": 2.1, "verdict": "STRONG",
                  "trend": "rising", "reason": "Trigger bar volume 2.1x."}
    vol_weak = {"symbol": "NIFTY", "rel_vol": 0.5, "verdict": "WEAK",
                "trend": "falling", "reason": "Trigger bar volume 0.5x."}
    cases = [
        ("clean long", dict(news_result=news_ok, mc_result=mc_good,
                            volume_result=vol_strong)),
        ("bad news long", dict(news_result=news_bad, mc_result=mc_good,
                               volume_result=vol_strong)),
        ("bad MC long", dict(news_result=news_ok, mc_result=mc_bad,
                             volume_result=vol_strong)),
        ("weak volume long", dict(news_result=news_ok, mc_result=mc_good,
                                  volume_result=vol_weak)),
    ]
    for name, kwargs in cases:
        sig = entry_signal("NIFTY", "long", 25730.0, 25702.5, 25785.0,
                           support=25687.0, resistance=25800.0, **kwargs)
        print(f"{name}: {sig['decision']} (size {sig['size_factor']}) — {sig['summary']}")
    ex = exit_signal("NIFTY", "long", 25730.0, news_result=news_bad)
    print(f"exit on bad news: {ex['action']} — {ex['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
