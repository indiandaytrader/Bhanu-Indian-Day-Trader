<!-- docs/guides/broker.md -->
# Optional Upstox read-only feed

Set UPSTOX_ACCESS_TOKEN in the process environment and restart. If public NSE chain retrieval fails, the app tries the broker’s market-data GET endpoint.

The fallback covers current-week NIFTY or current-month BANKNIFTY. Where the provider omits an exchange timestamp, model Greeks remain unavailable. With no token the public feed retains its normal error state.

[All feature guides](README.md)
