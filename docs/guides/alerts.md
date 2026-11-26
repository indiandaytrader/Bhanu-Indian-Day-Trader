<!-- docs/guides/alerts.md -->
# Telegram and Discord alerts

Before starting, set TELEGRAM_WEBHOOK_URL to your bot’s HTTPS sendMessage endpoint and TELEGRAM_CHAT_ID to your destination. Start the bot conversation first. Do not commit the token or put it in the dashboard URL.

Telegram receives completed ORB breakouts, weekday 15:15 flat reminders, level proximity, daily paper risk-budget exhaustion and newly discovered news. Proximity requires a same-day cached level and an index tick less than 120 seconds old within 0.1% of that level. Risk uses planned entry risk allocated today against max_daily_loss in data/config.json. It is not a realised-loss alert.

DISCORD_WEBHOOK_URL enables ORB and flat reminders on Discord. Both integrations default to off. Successful deliveries are deduplicated in data/alert_delivery.sqlite. Offline hosts can miss alerts; an uncertain network acknowledgement can cause a repeat.

[All feature guides](README.md)
