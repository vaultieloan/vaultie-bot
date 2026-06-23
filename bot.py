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
import os, re, time, logging
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
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
ASK_TOKEN, ASK_AMOUNT, ASK_TERM, CONFIRM = range(4)

# loan terms (fallback; refreshed from GET /api/terms). shorter = cheaper.
TERMS = [
    {"key": "2h",  "label": "2 hours", "interest": 0.02},
    {"key": "1d",  "label": "1 day",   "interest": 0.04},
    {"key": "1w",  "label": "1 week",  "interest": 0.07},
    {"key": "1mo", "label": "1 month", "interest": 0.12},
]
DEFAULT_TERM = "1w"

def term_by(key):
    for t in TERMS:
        if t["key"] == key:
            return t
    for t in TERMS:
        if t["key"] == DEFAULT_TERM:
            return t
    return TERMS[0]

async def refresh_terms():
    global TERMS, DEFAULT_TERM
    try:
        d = await _get("/terms")
        if d.get("terms"):
            TERMS = d["terms"]
            DEFAULT_TERM = d.get("default", DEFAULT_TERM)
    except Exception:
        pass


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
        [InlineKeyboardButton("📖 How it works", callback_data="help"),
         InlineKeyboardButton("⏱ Terms", callback_data="terms")],
        [InlineKeyboardButton("🌐 Open app", url=f"{SITE}/markets.html"),
         InlineKeyboardButton("📄 Docs", url=f"{SITE}/docs.html")],
    ])

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🪶 *Vaultie* — the lending desk for your Pump.fun bags.\n\n"
        "Lock a bonded token → draw SOL → repay to unlock. *No selling, ever.*\n\n"
        "• Up to 25% LTV · interest fixed by term\n"
        "• Overcollateralized — a dip won't wipe you\n"
        "• Custodial MVP — high risk, size accordingly\n\n"
        "Tap below, or type /borrow to start.",
        parse_mode=ParseMode.MARKDOWN, reply_markup=menu())

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.callback_query.message if update.callback_query else update.effective_message
    if update.callback_query: await update.callback_query.answer()
    await m.reply_text(
        "*How Vaultie works*\n\n"
        "1️⃣ */borrow* — paste a bonded Pump.fun token, choose how many to lock and a term.\n"
        "2️⃣ Send the tokens to the lock address the bot gives you.\n"
        "3️⃣ A *SOL credit* is sent back to the wallet you sent from — automatically, on-chain.\n"
        "4️⃣ */positions* → *Repay* to send SOL back and unlock your tokens.\n\n"
        "*Good to know*\n"
        "• Interest is fixed by your term (2h–1mo); shorter is cheaper.\n"
        "• Miss the term, or price drops −50% → collateral is liquidated.\n"
        "• No wallet connection — the deposit *is* the action.\n"
        "• Custodial MVP: you trust the operator, not a contract. High risk.\n\n"
        "Quick commands: /borrow · /positions · /repay · /terms · /stats",
        parse_mode=ParseMode.MARKDOWN, reply_markup=menu())

async def terms_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.callback_query.message if update.callback_query else update.effective_message
    if update.callback_query: await update.callback_query.answer()
    await refresh_terms()
    rows = "\n".join(
        f"• *{t['label']}* — {t['interest']*100:.0f}% interest"
        + ("  _(default)_" if t["key"] == DEFAULT_TERM else "")
        for t in TERMS)
    await m.reply_text(
        "*Loan terms*\n\n" + rows +
        "\n\nRepay credit + interest before the term ends to unlock. "
        "Miss it and the collateral is forfeited.\n\nStart with /borrow.",
        parse_mode=ParseMode.MARKDOWN)

async def repay_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "To repay, send the *wallet you borrowed from* — I'll pull up your open loans "
        "with a Repay button for each.", parse_mode=ParseMode.MARKDOWN)
    ctx.user_data["awaiting_wallet"] = True

async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    try:
        s = await _get("/protocol/stats")
        txt = ("*Vaultie — live stats*\n\n"
               f"SOL in liquidity: *{s.get("liquiditySol",0):,.2f} SOL*\n"
               f"Credit outstanding: *{s.get("creditOutstandingSol",0):,.2f} SOL*\n"
               f"Open positions: *{s.get('activeLiens',0)}*\n"
               f"LP APR: *{s.get('lpApr',0)*100:.1f}%*")
    except Exception:
        txt = "Couldn't reach the backend right now. Try again shortly."
    await m.reply_text(txt, parse_mode=ParseMode.MARKDOWN)


# ----------------- positions -----------------
STATUS_EMOJI = {"pending_deposit": "⌛", "pending_approval": "⌛", "active": "🟢",
                "awaiting_repayment": "🟡", "repaid": "✅", "liquidated": "🔻",
                "defaulted": "⛔", "refunded": "↩️", "held": "⏸"}

def _time_left(due):
    if not due:
        return ""
    s = due - time.time()
    if s <= 0:
        return "⚠️ overdue"
    h, m = int(s // 3600), int((s % 3600) // 60)
    if h >= 24:
        return f"⏳ {h // 24}d {h % 24}h left"
    if h:
        return f"⏳ {h}h {m}m left"
    return f"⏳ {m}m left"

def _position_view(l):
    st = l.get("status", "?")
    sym = l.get("symbol", "?")
    em = STATUS_EMOJI.get(st, "•")
    lines = [f"{em} *${sym}* — _{st.replace('_', ' ')}_",
             f"Collateral: *{l.get('amount', 0):,.0f} ${sym}*",
             f"Credit drawn: *{l.get('creditSol', 0):.4f} SOL*",
             f"Repay to unlock: *{l.get('repaySol', 0):.4f} SOL*"]
    if l.get("termLabel"):
        tl = _time_left(l.get("dueAt"))
        lines.append(f"Term: *{l['termLabel']}*" + (f"  ·  {tl}" if tl else ""))
    kb = None
    if st == "active":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"💰 Repay ${sym}", callback_data=f"repay:{l['id']}")]])
    elif st == "awaiting_repayment":
        lines.append(f"\n➡️ Send *{l.get('repaySol', 0):.4f} SOL* to:\n`{l.get('repayAddress', '—')}`\n"
                     f"_Collateral unlocks automatically once it lands._")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh status", callback_data=f"refresh:{l['id']}")]])
    return "\n".join(lines), kb

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
    ctx.user_data["wallet"] = wallet
    try:
        data = await _get("/loans", {"recipient": wallet})
    except Exception:
        await update.effective_message.reply_text("Couldn't load positions right now. Try again."); return
    loans = data.get("loans", data) if isinstance(data, dict) else data
    if not loans:
        await update.effective_message.reply_text(
            "No positions for that wallet. Open one with /borrow."); return
    open_st = ("pending_deposit", "pending_approval", "active", "awaiting_repayment", "held")
    open_loans = [l for l in loans if l.get("status") in open_st]
    closed = [l for l in loans if l.get("status") not in open_st]
    await update.effective_message.reply_text(
        f"*Positions for* `{wallet[:4]}…{wallet[-4:]}`", parse_mode=ParseMode.MARKDOWN)
    for l in open_loans[:15]:
        text, kb = _position_view(l)
        await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    if closed:
        tail = "\n".join(f"{STATUS_EMOJI.get(l.get('status'),'•')} ${l.get('symbol','?')} — "
                         f"_{l.get('status','?').replace('_',' ')}_" for l in closed[:10])
        await update.effective_message.reply_text("*Closed*\n" + tail, parse_mode=ParseMode.MARKDOWN)
    if not open_loans:
        await update.effective_message.reply_text("No open positions. Open one with /borrow.")

async def on_repay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    loan_id = q.data.split(":", 1)[1]
    try:
        r = await _post(f"/loans/{loan_id}/repay", {})
    except Exception as e:
        log.warning("repay failed: %s", e)
        await q.message.reply_text("Couldn't start repayment right now. Try again in a moment."); return
    sym = r.get("symbol", "?")
    if r.get("status") == "awaiting_repayment":
        await q.message.reply_text(
            f"*Repay ${sym}* 🟡\n\n"
            f"Send exactly *{r.get('repaySol', 0):.4f} SOL* to:\n\n"
            f"`{r.get('repayAddress', '—')}`\n\n"
            f"From your own wallet. Your *{sym}* collateral is released automatically once the SOL "
            f"arrives (usually under a minute). Use /positions → 🔄 to check status.",
            parse_mode=ParseMode.MARKDOWN)
    elif r.get("status") == "repaid":
        await q.message.reply_text(f"✅ Repaid — your ${sym} collateral is on its way back to your wallet.")
    else:
        await q.message.reply_text("Repayment requested.")

async def on_refresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    loan_id = q.data.split(":", 1)[1]
    wallet = ctx.user_data.get("wallet")
    if not wallet:
        await q.answer("Send /positions again", show_alert=False); return
    await q.answer("Refreshing…")
    try:
        data = await _get("/loans", {"recipient": wallet})
    except Exception:
        await q.message.reply_text("Couldn't refresh. Try /positions again."); return
    loans = data.get("loans", data) if isinstance(data, dict) else data
    l = next((x for x in loans if x.get("id") == loan_id), None)
    if not l:
        await q.message.reply_text("Position not found."); return
    text, kb = _position_view(l)
    try:
        await q.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except Exception:
        await q.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


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
    ctx.user_data["amount"] = amount
    await refresh_terms()
    rows, row = [], []
    for tm in TERMS:
        row.append(InlineKeyboardButton(f"{tm['label']} · {int(tm['interest']*100)}%",
                                        callback_data=f"term:{tm['key']}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    await update.effective_message.reply_text(
        f"*Choose your borrow term — ${t['symbol']}*\n\n"
        f"Shorter term = lower interest. "
        f"*If you don't repay within the term, your collateral is forfeited.*",
        parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(rows))
    return ASK_TERM

async def got_term(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    key = q.data.split(":", 1)[1]
    term = term_by(key)
    ctx.user_data["term"] = term
    t = ctx.user_data["token"]; amount = ctx.user_data["amount"]
    value_usd = amount * t["priceUsd"]
    credit_sol = amount * t["priceSol"] * LTV
    repay_sol = credit_sol * (1 + term["interest"])
    liq_price = t["priceSol"] * (1 - LIQ_DROP)
    ctx.user_data["quote"] = {"credit": credit_sol, "repay": repay_sol, "liq": liq_price}
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm", callback_data="confirm"),
        InlineKeyboardButton("✖ Cancel", callback_data="cancel")]])
    await q.message.reply_text(
        f"*Quote — ${t['symbol']}*\n\n"
        f"Lock: *{amount:,.0f} ${t['symbol']}* (~${value_usd:,.0f})\n"
        f"LTV: *10%*\n"
        f"Term: *{term['label']}*  ·  interest *{int(term['interest']*100)}%*\n"
        f"You receive: *{credit_sol:.4f} SOL*\n"
        f"Repay to unlock: *{repay_sol:.4f} SOL*\n"
        f"Liquidation price: *{liq_price:.8f} SOL*\n\n"
        f"⚠ Not repaid within *{term['label']}* → collateral forfeited.\n"
        f"_Final credit is recalculated from the live price when your tokens arrive._",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    return CONFIRM

async def confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "cancel":
        await q.message.reply_text("Cancelled.")
        return ConversationHandler.END
    t = ctx.user_data["token"]; amount = ctx.user_data["amount"]
    term = ctx.user_data.get("term") or term_by(DEFAULT_TERM)
    try:
        loan = await _post("/loans", {"tokenAddress": t["address"], "symbol": t["symbol"],
                                      "amount": amount, "term": term["key"]})
    except Exception as e:
        await q.message.reply_text("Couldn't open the position right now. Try again later.")
        log.warning("open loan failed: %s", e)
        return ConversationHandler.END
    lock = loan.get("lockAddress", "—")
    await q.message.reply_text(
        f"*Position opened* ✅\n\n"
        f"Send *{amount:,.0f} ${t['symbol']}* from *your own wallet* to:\n\n"
        f"`{lock}`\n\n"
        f"Term: *{term['label']}* — repay within this window or your collateral is forfeited.\n"
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
    if q.data == "help": return await help_cmd(update, ctx)
    if q.data == "terms": return await terms_cmd(update, ctx)


async def _post_init(app):
    await app.bot.set_my_commands([
        BotCommand("borrow",    "Lock a token, draw SOL"),
        BotCommand("positions", "View your loans"),
        BotCommand("repay",     "Repay a loan to unlock"),
        BotCommand("terms",     "Loan terms & interest"),
        BotCommand("stats",     "Protocol stats"),
        BotCommand("help",      "How Vaultie works"),
    ])
    log.info("bot commands registered")


def main():
    if not TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN")
    app = Application.builder().token(TOKEN).post_init(_post_init).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("borrow", borrow_entry),
                      CallbackQueryHandler(borrow_entry, pattern="^borrow$")],
        states={
            ASK_TOKEN:  [MessageHandler(filters.TEXT & ~filters.COMMAND, got_token)],
            ASK_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_amount)],
            ASK_TERM:   [CallbackQueryHandler(got_term, pattern="^term:")],
            CONFIRM:    [CallbackQueryHandler(confirm, pattern="^(confirm|cancel)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("terms", terms_cmd))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("positions", positions_entry))
    app.add_handler(CommandHandler("repay", repay_entry))
    app.add_handler(CallbackQueryHandler(route_buttons, pattern="^(stats|positions|help|terms)$"))
    app.add_handler(CallbackQueryHandler(on_repay, pattern="^repay:"))
    app.add_handler(CallbackQueryHandler(on_refresh, pattern="^refresh:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, maybe_wallet))
    log.info("Vaultie bot up. API=%s", API)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
