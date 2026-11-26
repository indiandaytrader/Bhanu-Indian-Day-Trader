<!-- docs/guides/lab.md -->
# Strategy Backtesting Lab

In Time Machine, select a CSV for one underlying, a date range and strategy. Enter units, brokerage in rupees per order and slippage in basis points on each side. Click Run replay.

CSV headers: `time,open,high,low,close`. Use ISO bar-open timestamps; naive timestamps mean IST. Limit 8 MiB. Sessions need consecutive five-minute bars from 09:15 through 15:10. Incomplete days are counted and skipped.

The 15- and 30-minute ORB strategies fix the opening range. Rolling six-bar breakout updates its channel from the six preceding completed bars. Each strategy takes at most one breakout per day, at the confirming close. Stop is the range midpoint and target is 2R. Exit checks begin on the next bar. Ambiguous bars take the stop, and remaining positions flatten by 15:15.

Net P&L deducts two brokerage charges and adverse entry/exit slippage. Drawdown is the rupee decline from the previous peak of cumulative closed-trade net P&L, starting at zero. Intratrade drawdown, taxes and exchange fees are excluded.

[All feature guides](README.md)
