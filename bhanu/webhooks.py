# bhanu/webhooks.py
"""Optional outbound paper alerts. Only this background job performs delivery."""
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, build_opener
from zoneinfo import ZoneInfo

from bhanu.upstox import NoRedirect

IST = ZoneInfo("Asia/Kolkata")
DB = Path(__file__).resolve().parent.parent / "data" / "alert_delivery.sqlite"


def events(now, hits):
    if now.weekday() >= 5:
        return
    day = now.date().isoformat()
    minute = now.hour * 60 + now.minute
    if 915 <= minute < 920:
        yield day + "/flat", "15:15 IST · Paper positions flat karne ka reminder."
    if not 570 <= minute < 915:
        return
    for hit in hits:
        if hit.get("date") != day or hit.get("direction") not in ("long", "short"):
            continue
        try:
            close = datetime.fromisoformat(day + "T" + hit["bar_time"]).replace(tzinfo=IST) + timedelta(minutes=5)
        except (KeyError, ValueError, TypeError):
            continue
        if not 0 <= (now-close).total_seconds() < 600:
            continue
        symbol = hit["symbol"]
        side = "CALL" if hit["direction"] == "long" else "PUT"
        yield f"{day}/{symbol}/{side}", f"Paper ORB · {symbol} {side} · close {hit['close']} at {close:%H:%M} IST"


def snapshot_alerts(now, levels, ticks, news, risk_taken, risk_limit):
    """Build notifications from existing snapshots; never fetch prices or news."""
    day = now.date().isoformat()
    if risk_limit is not None and risk_limit > 0 and risk_taken >= risk_limit:
        yield day + "/risk", (f"Paper risk budget reached · allocated ₹{risk_taken:,.2f} "
                              f"of ₹{risk_limit:,.2f} today (planned risk, not realised loss).")
    for item in news:
        if item.get("id") and item.get("title"):
            yield "news/" + item["id"], (f"Newly discovered headline · {item.get('source') or 'source unknown'}"
                                         f" · published {item.get('published') or 'time unknown'}"
                                         f" · {item['title'][:500]}")
    if now.weekday() >= 5 or not 555 <= now.hour * 60 + now.minute < 915:
        return
    for row in levels:
        symbol = row["symbol"]
        tick = ticks.get(symbol) or {}
        if row.get("date") != day or not 0 <= now.timestamp() - (tick.get("time") or 0) < 120:
            continue
        price = tick.get("ltp")
        if not price or price <= 0:
            continue
        for key, label in (("nearest_support", "support"), ("nearest_resistance", "resistance")):
            level = row.get(key)
            if level and abs(level - price) <= price * 0.001:
                stamp = datetime.fromtimestamp(tick["time"], IST)
                yield (f"{day}/{symbol}/{label}/{level:.2f}",
                       f"Paper {symbol} near cached {label} {level:.2f} · "
                       f"quote {price:.2f} at {stamp:%H:%M:%S} IST")


def send(channel, url, message):
    parsed = urlsplit(url)
    host, prefix = ("discord.com", "/api/webhooks/") if channel == "discord" else ("api.telegram.org", "/bot")
    if parsed.scheme != "https" or parsed.netloc != host or not parsed.path.startswith(prefix):
        raise ValueError("Invalid alert endpoint")
    if channel == "telegram":
        if not parsed.path.endswith("/sendMessage") or not os.environ.get("TELEGRAM_CHAT_ID"):
            raise ValueError("Missing Telegram chat or sendMessage endpoint")
        body = {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": message}
    else:
        body = {"content": message, "allowed_mentions": {"parse": []}}
    req = Request(url, data=json.dumps(body).encode(), headers={"Content-Type":"application/json",
                  "User-Agent":"BhanuPaperDashboard/1.0"})
    with build_opener(NoRedirect()).open(req, timeout=5) as response:
        if channel == "telegram" and not json.load(response).get("ok"):
            raise ValueError("Telegram delivery failed")


def deliver(now, hits, telegram_events=()):
    channels = [(k, os.environ.get(k.upper()+"_WEBHOOK_URL")) for k in ("telegram","discord")]
    pending = list(events(now, hits)) if any(url for _,url in channels) else []
    extras = list(telegram_events) if os.environ.get("TELEGRAM_WEBHOOK_URL") else []
    if not pending and not extras:
        return 0
    DB.parent.mkdir(parents=True, exist_ok=True)
    failures = 0
    with closing(sqlite3.connect(DB)) as db:
        db.execute("CREATE TABLE IF NOT EXISTS sent(channel TEXT,event TEXT,PRIMARY KEY(channel,event))")
        for channel, url in channels:
            if not url:
                continue
            for key, message in pending + (extras if channel == "telegram" else []):
                if db.execute("SELECT 1 FROM sent WHERE channel=? AND event=?", (channel,key)).fetchone():
                    continue
                try:
                    send(channel,url,message)
                except (OSError, ValueError):
                    failures += 1
                    continue
                with db:
                    db.execute("INSERT OR IGNORE INTO sent VALUES (?,?)",(channel,key))
    return failures
