"""
Vaultie Telegram bot
====================
Borrow SOL against bonded Pump.fun tokens straight from Telegram.
It talks to the same Vaultie backend API (no private keys here).

Env:
    TELEGRAM_BOT_TOKEN   from @BotFather (required)
    VAULTIE_API          backend base, e.g. https://web-production-xxxx.up.railway.app/api
    VAULTIE_BANK_LINK    optional: link shown in /start (site)

Run:  python bot.py   (long-polling; deploy as a Railway "worker" service)
"""
import os, re, logging
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (Application, CommandHandler, MessageHandler, CallbackQueryHandler,
                          ConversationHandler, ContextTypes, filters)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vaultie.bot")

API  = os.getenv("VAULTIE_API", "http://localhost:8000/api").rstrip("/")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
SITE = os.getenv("VAULTIE_SITE", "https://vaultie.fun")

# protocol params (mirror backend defaults; quote is recomputed on-chain at deposit)
LTV, LTV_BOOST, INTEREST, LIQ_DROP, CAP = 0.10, 0.15, 0.05, 0.50, 0.10
ASK_TOKEN, ASK_AMOUNT, CONFIRM = range(3)


# ----------------- data helpers -----------------
async def _get(path, params=None):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(API + path, params=params); r.raise_for_status(); return r.json()

async def _post(path, json):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(API + path, json=json); r.raise_for_status(); return r.json()

async def dexscreener(address):
    """Direct token lookup so the bot works even if the backend can't reach Dexscreener."""
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get("https://api.dexscreener.com/latest/dex/tokens/" + address)
        r.raise_for_status(); d = r.json()
    pairs = [p for p in (d.get("pairs") or []) if p.get("chainId") == "solana"]
    if not pairs:
        return None
    p = max(pairs, key=lambda x: (x.get("liquidity") or {}).get("usd") or 0)
    price_usd = float(p.get("priceUsd") or 0)
    price_nat = float(p.get("priceNative") or 0)
    sol_quote = (p.get("quoteToken") or {}).get("symbol") in ("SOL", "WSOL")
    sol_usd = price_usd / price_nat if (sol_quote and price_nat) else 165.0
    price_sol = price_nat if (sol_quote and price_nat) else (price_usd / sol_usd)
    liq_usd = (p.get("liquidity") or {}).get("usd") or 0
    return {
        "symbol": (p.get("baseToken") or {}).get("symbol") or address[:4].upper(),
        "name": (p.get("baseToken") or {}).get("name") or "Token",
        "address": address, "priceUsd": price_usd, "priceSol": price_sol,
        "liquidityUsd": liq_usd,
    }

async def lookup_token(address):
    try:
        return await dexscreener(address)
    except Exception:
        try:
            return await _get("/tokens/lookup", {"address": address})
        except Exception:
            return None

def fmtp(n):
    n = float(n or 0)
    if n == 0: return "$0"
    if n >= 1: return f"${n:,.2f}"
    if n >= 0.01: return f"${n:.4f}"
    import math
    dec = min(12, -math.floor(math.log10(n)) + 3)
    return "$" + f"{n:.{dec}f}".rstrip("0")


# ----------------- commands -----------------
def menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Borrow SOL", callback_data="borrow")],
        [InlineKeyboardButton("📊 My positions", callback_data="positions"),
         InlineKeyboardButton("📈 Stats", callback_data="stats")],
        [InlineKeyboardButton("📄 Docs", url=f"{SITE}/docs.html"),
         InlineKeyboardButton("🌐 Open app", url=f"{SITE}/markets.html")],
    ])

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "*Vaultie* — draw SOL against your Pump.fun tokens, without selling.\n\n"
        "Lock a bonded token, get a SOL credit, repay to unlock. Custodial MVP — high risk.\n\n"
        "What do you want to do?",
        parse_mode=ParseMode.MARKDOWN, reply_markup=menu())

async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    try:
        s = await _get("/protocol/stats")
        txt = ("*Vaultie — live stats*\n\n"
               f"SOL in liquidity: *{s.get('liquiditySol',0):,.2f} ◎*\n"
               f"Credit outstanding: *{s.get('creditOutstandingSol',0):,.2f} ◎*\n"
               f"Open positions: *{s.get('activeLiens',0)}*\n"
               f"LP APR: *{s.get('lpApr',0)*100:.1f}%*")
    except Exception:
        txt = "Couldn't reach the backend right now. Try again shortly."
    await m.reply_text(txt, parse_mode=ParseMode.MARKDOWN)


# ----------------- positions -----------------
async def positions_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.callback_query.message if update.callback_query else update.effective_message
    if update.callback_query: await update.callback_query.answer()
    await msg.reply_text("Send the *Solana wallet address* you borrowed from:", parse_mode=ParseMode.MARKDOWN)
    ctx.user_data["awaiting_wallet"] = True

async def maybe_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("awaiting_wallet"):
        return
    ctx.user_data["awaiting_wallet"] = False
    wallet = update.effective_message.text.strip()
    try:
        data = await _get("/loans", {"recipient": wallet})
        loans = data.get("loans", data) if isinstance(data, dict) else data
        if not loans:
            await update.effective_message.reply_text("No positions found for that wallet.")
            return
        lines = ["*Your positions*\n"]
        for l in loans[:10]:
            lines.append(
                f"• *${l.get('symbol','?')}* — {l.get('status','?')}\n"
                f"   credit {l.get('creditSol',0)} ◎ · repay {l.get('repaySol',0)} ◎")
        await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await update.effective_message.reply_text("Couldn't load positions right now.")


# ----------------- borrow conversation -----------------
async def borrow_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.callback_query.message if update.callback_query else update.effective_message
    if update.callback_query: await update.callback_query.answer()
    await msg.reply_text("Paste the *bonded Pump.fun token address* you want to borrow against:",
                         parse_mode=ParseMode.MARKDOWN)
    return ASK_TOKEN

async def got_token(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    addr = update.effective_message.text.strip()
    if len(addr) < 32:
        await update.effective_message.reply_text("That doesn't look like a token address. Try again, or /cancel.")
        return ASK_TOKEN
    await update.effective_message.reply_text("Looking it up…")
    t = await lookup_token(addr)
    if not t:
        await update.effective_message.reply_text(
            "No bonded market found for that token. Vaultie only lends against *bonded* "
            "Pump.fun tokens (migrated to an AMM). Try another, or /cancel.",
            parse_mode=ParseMode.MARKDOWN)
        return ASK_TOKEN
    ctx.user_data["token"] = t
    max_lock = (t.get("liquidityUsd") or 0) * CAP
    await update.effective_message.reply_text(
        f"*${t['symbol']}* — {t['name']}\n"
        f"Price: *{fmtp(t['priceUsd'])}*\n"
        f"Pool liquidity: *${t.get('liquidityUsd',0):,.0f}*\n"
        f"Max lockable (Smart Cap 10%): *${max_lock:,.0f}*\n\n"
        f"How many *${t['symbol']}* do you want to lock?",
        parse_mode=ParseMode.MARKDOWN)
    return ASK_AMOUNT

async def got_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.effective_message.text.strip().replace(",", "").replace("_", "")
    try:
        amount = float(raw)
        assert amount > 0
    except Exception:
        await update.effective_message.reply_text("Send a number, e.g. 1000000. Or /cancel.")
        return ASK_AMOUNT
    t = ctx.user_data["token"]
    value_usd = amount * t["priceUsd"]
    max_lock = (t.get("liquidityUsd") or 0) * CAP
    if max_lock and value_usd > max_lock:
        await update.effective_message.reply_text(
            f"That exceeds the Smart Cap (max ~${max_lock:,.0f}). Send a smaller amount.")
        return ASK_AMOUNT
    credit_sol = amount * t["priceSol"] * LTV
    repay_sol = credit_sol * (1 + INTEREST)
    liq_price = t["priceSol"] * (1 - LIQ_DROP)
    ctx.user_data["amount"] = amount
    ctx.user_data["quote"] = {"credit": credit_sol, "repay": repay_sol, "liq": liq_price}
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm", callback_data="confirm"),
        InlineKeyboardButton("✖ Cancel", callback_data="cancel")]])
    await update.effective_message.reply_text(
        f"*Quote — ${t['symbol']}*\n\n"
        f"Lock: *{amount:,.0f} ${t['symbol']}* (~${value_usd:,.0f})\n"
        f"LTV: *10%*\n"
        f"You receive: *{credit_sol:.4f} ◎*\n"
        f"Repay to unlock: *{repay_sol:.4f} ◎* (×1.05)\n"
        f"Liquidation price: *{liq_price:.8f} ◎*\n\n"
        f"_Final credit is recalculated from the live price when your tokens arrive._",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    return CONFIRM

async def confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "cancel":
        await q.message.reply_text("Cancelled.")
        return ConversationHandler.END
    t = ctx.user_data["token"]; amount = ctx.user_data["amount"]
    try:
        loan = await _post("/loans", {"tokenAddress": t["address"], "symbol": t["symbol"], "amount": amount})
    except Exception as e:
        await q.message.reply_text("Couldn't open the position right now. Try again later.")
        log.warning("open loan failed: %s", e)
        return ConversationHandler.END
    lock = loan.get("lockAddress", "—")
    await q.message.reply_text(
        f"*Position opened* ✅\n\n"
        f"Send *{amount:,.0f} ${t['symbol']}* from *your own wallet* to:\n\n"
        f"`{lock}`\n\n"
        f"The SOL credit is paid back to the wallet you send from — no address needed. "
        f"Track it any time with *My positions*.\n\n"
        f"⚠ Send only ${t['symbol']} to this address, from a wallet you control (not an exchange).",
        parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END

async def route_buttons(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.data == "stats": await q.answer(); return await stats(update, ctx)
    if q.data == "positions": return await positions_entry(update, ctx)


def main():
    if not TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN")
    app = Application.builder().token(TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("borrow", borrow_entry),
                      CallbackQueryHandler(borrow_entry, pattern="^borrow$")],
        states={
            ASK_TOKEN:  [MessageHandler(filters.TEXT & ~filters.COMMAND, got_token)],
            ASK_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_amount)],
            CONFIRM:    [CallbackQueryHandler(confirm, pattern="^(confirm|cancel)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("positions", positions_entry))
    app.add_handler(CallbackQueryHandler(route_buttons, pattern="^(stats|positions)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, maybe_wallet))
    log.info("Vaultie bot up. API=%s", API)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
