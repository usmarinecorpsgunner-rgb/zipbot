#!/usr/bin/env python3
"""
Telegram bot — ZIP code median income lookup with ETH payment gate.
$1 = 7-day subscription, paid in ETH, verified via Etherscan API.

Env vars required:
  TELEGRAM_TOKEN    - from @BotFather
  CENSUS_API_KEY    - from api.census.gov
  ETHERSCAN_API_KEY - from etherscan.io (free)
  ADMIN_USER_ID     - your Telegram user ID (gets free access)
"""

import os, json, logging, requests, secrets, string
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN_HERE")
CENSUS_API_KEY    = os.getenv("CENSUS_API_KEY", "")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")
ADMIN_USER_ID     = os.getenv("ADMIN_USER_ID", "")
YOUR_ETH_WALLET   = "0xa00dbAF96a1bC5fa13868E2876B6e8303CeCd11D"
PRICE_USD         = 1.00
SUB_DAYS          = 7
DB_FILE           = "subscribers.json"

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Subscriber DB ─────────────────────────────────────────────────────────────
def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE) as f:
            return json.load(f)
    return {}

def save_db(db):
    with open(DB_FILE, "w") as f:
        json.dump(db, f)

def is_admin(user_id: str) -> bool:
    return ADMIN_USER_ID and str(user_id) == str(ADMIN_USER_ID)

def is_subscribed(user_id: str) -> bool:
    if is_admin(user_id):
        return True
    db = load_db()
    if user_id not in db:
        return False
    expiry = datetime.fromisoformat(db[user_id]["expiry"])
    return datetime.utcnow() < expiry

def add_subscription(user_id: str):
    db = load_db()
    expiry = datetime.utcnow() + timedelta(days=SUB_DAYS)
    db[user_id] = {"expiry": expiry.isoformat()}
    save_db(db)

def get_expiry(user_id: str) -> str:
    if is_admin(user_id):
        return "∞ (Admin — free forever)"
    db = load_db()
    if user_id not in db:
        return "No subscription"
    expiry = datetime.fromisoformat(db[user_id]["expiry"])
    if datetime.utcnow() > expiry:
        return "Expired"
    return expiry.strftime("%Y-%m-%d %H:%M UTC")

# ── ETH helpers ───────────────────────────────────────────────────────────────
def get_eth_price_usd() -> float:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "ethereum", "vs_currencies": "usd"},
            timeout=10
        )
        return float(r.json()["ethereum"]["usd"])
    except Exception:
        return float(os.getenv("ETH_PRICE_USD", "3000"))

def usd_to_eth(usd: float) -> float:
    price = get_eth_price_usd()
    return round(usd / price, 6)

# ── Etherscan verification ────────────────────────────────────────────────────
def verify_tx(tx_hash: str, expected_eth: float) -> tuple[bool, str]:
    params = {
        "module": "proxy",
        "action": "eth_getTransactionByHash",
        "txhash": tx_hash,
        "apikey": ETHERSCAN_API_KEY,
    }
    try:
        r = requests.get("https://api.etherscan.io/api", params=params, timeout=10)
        tx = r.json().get("result")
        if not tx:
            return False, "Transaction not found. Make sure it's confirmed on Ethereum mainnet."
        to_addr = (tx.get("to") or "").lower()
        if to_addr != YOUR_ETH_WALLET.lower():
            return False, "Transaction was not sent to the correct wallet."
        value_eth = int(tx.get("value", "0x0"), 16) / 1e18
        if value_eth < expected_eth * 0.95:
            return False, f"Amount too low. Received {value_eth:.6f} ETH, expected ~{expected_eth:.6f} ETH."
        if not tx.get("blockNumber"):
            return False, "Transaction is still pending. Please wait for confirmation and try again."
        return True, f"Verified! Received {value_eth:.6f} ETH ✅"
    except Exception as e:
        return False, f"Verification error: {e}"

# ── Census lookup ─────────────────────────────────────────────────────────────
def get_median_income(zip_code: str) -> str:
    params = {"get": "B19013_001E,NAME", "for": f"zip code tabulation area:{zip_code}"}
    if CENSUS_API_KEY:
        params["key"] = CENSUS_API_KEY
    try:
        r = requests.get("https://api.census.gov/data/2022/acs/acs5", params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return f"❌ Census lookup failed: {e}"
    if len(data) < 2:
        return f"❌ No data found for ZIP {zip_code}."
    income_raw, name = data[1][0], data[1][1]
    if income_raw in (None, "-666666666", "-999999999"):
        return f"⚠️ Data not available for ZIP {zip_code}."
    return (
        f"📍 *{name}*\n"
        f"💰 Median Household Income: *${int(income_raw):,.0f}*\n"
        f"_(Source: US Census ACS 5-Year Estimates, 2022)_"
    )

# ── Pay button keyboard ───────────────────────────────────────────────────────
def pay_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("💳 Pay $1 in ETH", callback_data="pay")]])

# ── Handlers ──────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    first_name = update.effective_user.first_name or "there"

    if is_subscribed(user_id):
        expiry = get_expiry(user_id)
        await update.message.reply_text(
            f"👋 Welcome back, *{first_name}*!\n\n"
            f"✅ Your subscription is active until *{expiry}*\n\n"
            f"📬 Just send any 5-digit US ZIP code and I'll look up the median household income for that area.\n\n"
            f"*Commands:*\n"
            f"• /status — check your subscription\n"
            f"• /pay — renew or pay\n"
            f"• /help — show this guide",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"👋 Hey *{first_name}*, welcome to *ZIP Income Bot*!\n\n"
            f"📊 *What this bot does:*\n"
            f"Send any US ZIP code and instantly get the median household income for that area — powered by US Census data.\n\n"
            f"*Example:* Send `90210` → get Beverly Hills income data\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💳 *How to get access:*\n"
            f"1️⃣ Click the button below\n"
            f"2️⃣ Send *$1 worth of ETH* to the wallet shown\n"
            f"3️⃣ Paste your transaction hash\n"
            f"4️⃣ Get *7 days* of unlimited lookups ✅\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"*Commands:*\n"
            f"• /pay — subscribe for $1\n"
            f"• /status — check your subscription\n"
            f"• /help — show this guide",
            parse_mode="Markdown",
            reply_markup=pay_keyboard()
        )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *ZIP Income Bot — Help Guide*\n\n"
        "*How to use:*\n"
        "1. Subscribe for $1 in ETH (7 days access)\n"
        "2. Send any 5-digit US ZIP code\n"
        "3. Get the median household income instantly\n\n"
        "*Commands:*\n"
        "• /start — welcome screen\n"
        "• /pay — subscribe or renew\n"
        "• /status — check your subscription expiry\n"
        "• /help — show this guide\n\n"
        "*Paying:*\n"
        "• Use /pay to get the ETH wallet + exact amount\n"
        "• After sending, paste your tx hash (0x...)\n"
        "• Bot verifies on Etherscan automatically\n\n"
        "*Data source:* US Census ACS 5-Year Estimates (2022)",
        parse_mode="Markdown",
        reply_markup=pay_keyboard()
    )

async def pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if is_subscribed(user_id):
        expiry = get_expiry(user_id)
        await update.message.reply_text(f"✅ You're already subscribed until *{expiry}*.", parse_mode="Markdown")
        return
    await send_pay_instructions(update.message.reply_text, context, user_id)

async def pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    if is_subscribed(user_id):
        expiry = get_expiry(user_id)
        await query.message.reply_text(f"✅ You're already subscribed until *{expiry}*.", parse_mode="Markdown")
        return
    await send_pay_instructions(query.message.reply_text, context, user_id)

async def send_pay_instructions(reply_fn, context, user_id):
    eth_amount = usd_to_eth(PRICE_USD)
    context.user_data["expected_eth"] = eth_amount
    await reply_fn(
        f"💳 *Payment Instructions*\n\n"
        f"Send exactly:\n"
        f"```\n{eth_amount:.6f} ETH\n```\n"
        f"To this wallet:\n"
        f"```\n{YOUR_ETH_WALLET}\n```\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ After sending, paste your *transaction hash* (starts with 0x) here to verify.\n\n"
        f"⏳ ETH amount is based on live price — run /pay again if you wait too long.",
        parse_mode="Markdown"
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if is_subscribed(user_id):
        expiry = get_expiry(user_id)
        await update.message.reply_text(f"✅ Active subscription until *{expiry}*.", parse_mode="Markdown")
    else:
        await update.message.reply_text(
            "❌ No active subscription.",
            reply_markup=pay_keyboard()
        )

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    text = update.message.text.strip()

    # Transaction hash
    if text.startswith("0x") and len(text) == 66:
        expected_eth = context.user_data.get("expected_eth") or usd_to_eth(PRICE_USD)
        await update.message.reply_text("🔍 Verifying your transaction on Etherscan...")
        ok, msg = verify_tx(text, expected_eth)
        if ok:
            add_subscription(user_id)
            expiry = get_expiry(user_id)
            await update.message.reply_text(
                f"🎉 *Payment confirmed!*\n\n"
                f"{msg}\n\n"
                f"✅ Subscription active until *{expiry}*\n\n"
                f"Now send any 5-digit ZIP code to get started!",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(f"❌ *Verification failed:*\n{msg}", parse_mode="Markdown")
        return

    # ZIP lookup — single or bulk (space/comma/newline separated)
    tokens = [t.strip().strip(",") for t in text.replace(",", " ").replace("\n", " ").split()]
    zips = [t for t in tokens if t.isdigit() and len(t) == 5]

    if zips:
        if not is_subscribed(user_id):
            await update.message.reply_text(
                "🔒 *Access required!*\n\nSubscribe for just $1 to unlock ZIP income lookups for 7 days.",
                parse_mode="Markdown",
                reply_markup=pay_keyboard()
            )
            return
        if len(zips) == 1:
            await update.message.reply_text("🔍 Looking up...")
            result = get_median_income(zips[0])
            await update.message.reply_text(result, parse_mode="Markdown")
        else:
            if len(zips) > 20:
                await update.message.reply_text("⚠️ Max 20 ZIPs at a time. Showing first 20.")
                zips = zips[:20]
            await update.message.reply_text(f"🔍 Looking up {len(zips)} ZIP codes...")
            results = []
            for z in zips:
                results.append(get_median_income(z))
            await update.message.reply_text("\n\n".join(results), parse_mode="Markdown")
        return

    # Redeem a free key
    if len(text) == 16 and text.isalnum():
        db = load_db()
        keys = db.get("_keys", {})
        if text in keys:
            if keys[text].get("used"):
                await update.message.reply_text("❌ That key has already been used.")
            else:
                keys[text]["used"] = True
                db["_keys"] = keys
                save_db(db)
                expiry = datetime.utcnow() + timedelta(days=keys[text].get("days", 1))
                db2 = load_db()
                db2[user_id] = {"expiry": expiry.isoformat()}
                save_db(db2)
                await update.message.reply_text(
                    f"🎉 *Key redeemed!*\n\n✅ Access granted until *{expiry.strftime('%Y-%m-%d %H:%M UTC')}*\n\nSend any ZIP code to get started!",
                    parse_mode="Markdown"
                )
        else:
            await update.message.reply_text("❌ Invalid key. Check it and try again.")
        return

    await update.message.reply_text(
        "Send one or more 5-digit ZIP codes (space separated) to look up income.\n\nExample: `90210 10001 30301`\n\nNeed access? Use /pay or redeem a key.",
        parse_mode="Markdown"
    )

# ── Key generation (admin only) ───────────────────────────────────────────────
def generate_key(days: int = 1) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(16))

async def genkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_admin(user_id):
        await update.message.reply_text("❌ Admin only command.")
        return

    # Parse days argument: /genkey 3  or /genkey (defaults to 1)
    days = 1
    if context.args:
        try:
            days = max(1, int(context.args[0]))
        except ValueError:
            pass

    # Parse quantity: /genkey 1 5  (1 day, 5 keys)
    qty = 1
    if len(context.args) >= 2:
        try:
            qty = max(1, min(20, int(context.args[1])))
        except ValueError:
            pass

    db = load_db()
    keys = db.get("_keys", {})
    new_keys = []
    for _ in range(qty):
        k = generate_key(days)
        keys[k] = {"days": days, "used": False}
        new_keys.append(k)
    db["_keys"] = keys
    save_db(db)

    key_list = "\n".join([f"`{k}`" for k in new_keys])
    await update.message.reply_text(
        f"🔑 *Generated {qty} key(s) — {days} day(s) each:*\n\n{key_list}\n\n"
        f"Share these with users. Each key is single-use.",
        parse_mode="Markdown"
    )

async def listkeys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_admin(user_id):
        await update.message.reply_text("❌ Admin only command.")
        return
    db = load_db()
    keys = db.get("_keys", {})
    if not keys:
        await update.message.reply_text("No keys generated yet.")
        return
    unused = [k for k, v in keys.items() if not v.get("used")]
    used = [k for k, v in keys.items() if v.get("used")]
    msg = f"🔑 *Keys*\n\n✅ Unused ({len(unused)}):\n"
    msg += "\n".join([f"`{k}` ({keys[k]['days']}d)" for k in unused]) or "None"
    msg += f"\n\n❌ Used ({len(used)}):\n"
    msg += "\n".join([f"`{k}`" for k in used]) or "None"
    await update.message.reply_text(msg, parse_mode="Markdown")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pay", pay))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("genkey", genkey))
    app.add_handler(CommandHandler("listkeys", listkeys))
    app.add_handler(CallbackQueryHandler(pay_callback, pattern="^pay$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    logger.info("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
