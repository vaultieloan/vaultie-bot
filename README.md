# Vaultie Telegram bot

Lets users borrow against bonded Pump.fun tokens from Telegram. Talks to the
Vaultie backend API. No keys here.

## Deploy (Railway, separate service)
1. New service from this folder (or repo subdir `bot`).
2. Variables:
   - `TELEGRAM_BOT_TOKEN`  — from @BotFather
   - `VAULTIE_API`         — backend base, e.g. https://web-production-e96e6.up.railway.app/api
   - `VAULTIE_SITE`        — optional, e.g. https://vaultie.fun
3. It long-polls (no webhook, no public port needed).

## Commands
/start · /borrow · /positions · /stats · /cancel
