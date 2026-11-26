"""Hinglish UI strings for the Bhanu Indian Day Trader dashboard.

Every string is keyed once and given in English (``"en"``) and in Hinglish
(``"hi"`` — Hindi in Roman script, the way Bhanu reads it). Use ``t(key)``
to fetch a string; the default language is Hinglish (``"hi"``), with a
fallback to English when a key or a language is missing.

Example::

    from bhanu.i18n import t
    t("paper_trading")            # "Paper Trading"
    t("paper_trading", "en")      # "Paper Trading"
    t("no_signal")                # "Koi signal nahi"
    t("missing_key")              # "missing_key" (key itself, never a crash)
"""

from __future__ import annotations

from typing import Literal

#: Supported language codes: English and Hinglish (Roman Hindi).
LANGUAGES = ["en", "hi"]

Lang = Literal["en", "hi"]

#: UI strings: key -> {"en": English, "hi": Hinglish in Roman script}.
STRINGS: dict[str, dict[str, str]] = {
    "app_title": {
        "en": "Bhanu Indian Day Trader",
        "hi": "Bhanu Indian Day Trader",
    },
    "scanner": {
        "en": "Scanner",
        "hi": "Scanner",
    },
    "option_chain": {
        "en": "Option Chain",
        "hi": "Option Chain",
    },
    "orb_scanner": {
        "en": "ORB Scanner",
        "hi": "ORB Scanner",
    },
    "levels": {
        "en": "Levels",
        "hi": "Levels",
    },
    "skills": {
        "en": "Skills",
        "hi": "Skills",
    },
    "timemachine": {
        "en": "Time Machine",
        "hi": "Time Machine",
    },
    "entry": {
        "en": "Entry",
        "hi": "Entry",
    },
    "exit": {
        "en": "Exit",
        "hi": "Exit",
    },
    "sizing": {
        "en": "Sizing",
        "hi": "Sizing",
    },
    "paper_trading": {
        "en": "Paper Trading",
        "hi": "Paper Trading",
    },
    "stop_loss": {
        "en": "Stop Loss",
        "hi": "Stop Loss",
    },
    "target": {
        "en": "Target",
        "hi": "Target",
    },
    "flat_by": {
        "en": "Flat by 15:15",
        "hi": "15:15 tak flat",
    },
    "long": {
        "en": "CALL",
        "hi": "CALL",
    },
    "short": {
        "en": "PUT",
        "hi": "PUT",
    },
    "no_signal": {
        "en": "No signal",
        "hi": "Koi signal nahi",
    },
    "loading": {
        "en": "Loading...",
        "hi": "Load ho raha hai...",
    },
    # Extra everyday UI strings (same en/hi pattern).
    "buy": {
        "en": "Buy",
        "hi": "Buy karo",
    },
    "sell": {
        "en": "Sell",
        "hi": "Sell karo",
    },
    "profit": {
        "en": "Profit",
        "hi": "Profit",
    },
    "loss": {
        "en": "Loss",
        "hi": "Loss",
    },
    "balance": {
        "en": "Balance",
        "hi": "Balance",
    },
    "risk": {
        "en": "Risk",
        "hi": "Risk",
    },
    "daily_summary": {
        "en": "Daily Summary",
        "hi": "Aaj ka summary",
    },
    "market_closed": {
        "en": "Market closed",
        "hi": "Market band hai",
    },
    "market_open": {
        "en": "Market open",
        "hi": "Market khula hai",
    },
    "no_trade_today": {
        "en": "No trade today",
        "hi": "Aaj koi trade nahi",
    },
}

__all__ = ["STRINGS", "LANGUAGES", "t"]


def t(key: str, lang: str = "hi") -> str:
    """Return the UI string for ``key`` in ``lang`` (default Hinglish).

    Falls back to English when the requested language is missing for the
    key, and returns the key itself when the key does not exist — so a
    missing string never crashes the UI.
    """
    entry = STRINGS.get(key)
    if entry is None:
        return key
    if lang in entry:
        return entry[lang]
    return entry.get("en", key)


def demo() -> None:
    """Offline demo: print every string in English and Hinglish."""
    print("Hinglish UI strings demo (offline, no network).")
    for key in STRINGS:
        print(f"  {key}: en={t(key, 'en')!r}  hi={t(key, 'hi')!r}")
    print(f"  fallback check: t('no_such_key') -> {t('no_such_key')!r}")
    print(f"  languages: {LANGUAGES}")


if __name__ == "__main__":
    demo()
