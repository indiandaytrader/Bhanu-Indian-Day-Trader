# bhanu/range_replay.py
"""Replay one underlying's supplied five-minute bars. Prices are rupees per unit."""
import csv
import io
from collections import defaultdict
from datetime import date, datetime
from math import isfinite
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
OPEN, FLAT = 555, 915


STRATEGIES = {"orb15": "15-minute ORB", "orb30": "30-minute ORB",
              "channel": "Rolling six-bar breakout"}


def replay(text, start, end, brokerage, slippage, quantity, strategy="orb15"):
    if strategy not in STRATEGIES:
        raise ValueError("Unknown strategy.")
    start, end = date.fromisoformat(start), date.fromisoformat(end)
    brokerage, slippage, quantity = float(brokerage), float(slippage), int(quantity)
    if start > end or not 0 <= brokerage <= 100000 or not 0 <= slippage < 10000 or not 1 <= quantity <= 1000000:
        raise ValueError("Check date order, brokerage, quantity and slippage (0–9999 bps).")
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    fields = ("open", "high", "low", "close")
    if not {"time", *fields}.issubset(reader.fieldnames or []):
        raise ValueError("CSV needs time,open,high,low,close headers.")
    sessions = defaultdict(dict)
    for row in reader:
        try:
            ts = datetime.fromisoformat(row["time"].replace("Z", "+00:00"))
            ts = ts.astimezone(IST) if ts.tzinfo else ts.replace(tzinfo=IST)
            minute = ts.hour * 60 + ts.minute
            if not start <= ts.date() <= end or not OPEN <= minute < FLAT:
                continue
            if ts.second or ts.microsecond or minute % 5 or minute in sessions[ts.date()]:
                raise ValueError
            o,h,l,c = [float(row[k]) for k in fields]
            if not all(isfinite(v) and 0 < v < 1e12 for v in (o,h,l,c)) or not l <= min(o,c) <= max(o,c) <= h:
                raise ValueError
            sessions[ts.date()][minute] = o,h,l,c
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError(f"Invalid or duplicate five-minute bar at CSV line {reader.line_num}.") from exc
    if not sessions:
        raise ValueError("No bars in this date range.")
    trades = []
    skipped = 0
    for day, bars in sorted(sessions.items()):
        if len(bars) != (FLAT - OPEN)//5:
            skipped += 1
            continue
        window = 15 if strategy == "orb15" else 30
        for minute in range(OPEN + window, FLAT - 5, 5):
            left = minute - window if strategy == "channel" else OPEN
            prior = range(left, left + window, 5)
            ceiling = max(bars[t][1] for t in prior)
            floor = min(bars[t][2] for t in prior)
            stop = (ceiling + floor) / 2
            entry = bars[minute][3]
            if floor <= entry <= ceiling:
                continue
            sign = 1 if entry > ceiling else -1
            target = entry + 2*(entry-stop)
            exit_price, reason = bars[FLAT-5][3], "15:15 flat"
            for t in range(minute+5, FLAT, 5):
                o,h,l,c = bars[t]
                if sign*(o-stop) <= 0:
                    exit_price, reason = o, "gap stop"
                elif sign*(o-target) >= 0:
                    exit_price, reason = target, "target"
                elif (l <= stop if sign == 1 else h >= stop):
                    exit_price, reason = stop, "stop"
                elif (h >= target if sign == 1 else l <= target):
                    exit_price, reason = target, "target"
                else:
                    continue
                break
            gross = sign*(exit_price-entry)*quantity
            cost = (entry+exit_price)*quantity*slippage/10000 + 2*brokerage
            trades.append({"date": str(day), "side": "CALL" if sign==1 else "PUT",
                           "exit": reason, "gross": gross, "cost": cost, "net": gross-cost})
            break
    equity = peak = max_drawdown = 0.0
    for trade in trades:
        equity += trade["net"]
        peak = max(peak, equity)
        drawdown = peak - equity
        max_drawdown = max(max_drawdown, drawdown)
        trade.update(equity=equity, drawdown=drawdown)
    return {"strategy": STRATEGIES[strategy], "max_drawdown": max_drawdown, "from": str(min(sessions)), "to": str(max(sessions)), "complete": len(sessions)-skipped,
            "skipped": skipped, "trades": trades}
