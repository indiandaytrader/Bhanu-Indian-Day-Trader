# bhanu/greeks.py
"""European Black-Scholes with explicit zero carry and ACT/365 assumptions."""
from datetime import datetime, time
from math import erf, exp, isfinite, log, pi, sqrt
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def years_left(as_of, expiry):
    try:
        try:
            stamp = datetime.fromisoformat(as_of)
        except ValueError:
            stamp = datetime.strptime(as_of, "%d-%b-%Y %H:%M:%S")
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=IST)
        end = datetime.combine(datetime.fromisoformat(expiry).date(), time(15, 30), IST)
        return (end - stamp).total_seconds() / (365 * 86400)
    except (ValueError, TypeError):
        return None


def solve(spot, strike, premium, years, call):
    """IV in percent, theta per day and vega per one volatility point."""
    if any(v is None or not isfinite(v) or v <= 0 for v in (spot, strike, premium, years)):
        return {}
    sign = 1 if call else -1
    if not max(0, sign * (spot - strike)) < premium < (spot if call else strike):
        return {}
    def cdf(x):
        return (1 + erf(x / sqrt(2))) / 2

    def terms(vol):
        width = vol * sqrt(years)
        d1 = log(spot / strike) / width + width / 2
        return d1, d1 - width

    def price(vol):
        d1, d2 = terms(vol)
        return sign * (spot * cdf(sign * d1) - strike * cdf(sign * d2))

    low, high = 0., 1.
    while high < 64 and price(high) < premium:
        high *= 2
    if price(high) < premium:
        return {}
    for _ in range(64):
        mid = (low + high) / 2
        if price(mid) < premium:
            low = mid
        else:
            high = mid
    vol = (low + high) / 2
    d1, _ = terms(vol)
    density = exp(-d1*d1/2) / sqrt(2*pi)
    return {"model_iv": 100 * vol, "delta": sign * cdf(sign*d1),
            "gamma": density / (spot * vol * sqrt(years)),
            "theta": -spot * density * vol / (2 * sqrt(years) * 365),
            "vega": spot * density * sqrt(years) / 100}
