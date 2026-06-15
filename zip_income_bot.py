#!/usr/bin/env python3
"""
Telegram bot — ZIP code median income lookup with ETH payment gate.
$1 = 7-day subscription, paid in ETH, verified via Etherscan API.

Env vars required:
  TELEGRAM_TOKEN   - from @BotFather
  CENSUS_API_KEY   - from api.census.gov
  ETHERSCAN_API_KEY - from etherscan.io (free)

Optional:
  ETH_PRICE_USD    - override ETH price (default: fetched live from CoinGecko)
"""

import os, json, time, logging, requests
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
YOUR_ETH_WALLET   = "0xa00dbAF96a1bC5fa13868E2876B6e8303CeCd11D"
PRICE_USD         = 1.00          # subscription price in USD
SUB_DAYS          = 7             # subscription length
DB_FILE           = "subscribers.json"  # simple local JSON store

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Subscriber DB (JSON file) ─────────────────────────────────────────────────
def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE) as f:
            return json.load(f)
    return {}

def save_db(db):
    with open(DB_FILE, "w") as f:
        json.dump(db, f)

def is_subscribed(user_id: str) -> bool:
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
    db = load_db()
    if user_id not in db:
        return "No subscription"
    expiry = datetime.fromisoformat(db[user_id]["expiry"])
    if datetime.utcnow() > expiry:
        return "Expired"
    return expiry.strftime("%Y-%m-%d %H:%M UTC")

# ── ETH price & amount ────────────────────────────────────────────────────────
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
    """Check Etherscan that tx_hash sends >= expected_eth to YOUR_ETH_WALLET."""
    url = "https://api.etherscan.io/api"
    params = {
        "module": "proxy",
        "action": "eth_getTransactionByHash",
        "txhash": tx_hash,
        "apikey": ETHERSCAN_API_KEY,
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        tx = data.get("result")
        if not tx:
            return False, "Transaction not found. Make sure it's confirmed on Ethereum mainnet."

        to_addr = (tx.get("to") or "").lower()
        if to_addr != YOUR_ETH_WALLET.lower():
            return False, f"Transaction was not sent to the correct wallet."

        value_wei = int(tx.get("value", "0x0"), 16)
        value_eth = value_wei / 1e18
        if value_eth < expected_eth * 0.95:  # 5% tolerance for gas/rounding
            return False, (
                f"Amount too low. Received {value_eth:.6f} ETH, "
                f"expected ~{expected_eth:.6f} ETH."
            )

        # Check tx is confirmed (has a block number)
        if not tx.get("blockNumber"):
            return False, "Transaction is still pending. Please wait for confirmation and try again."

        return True, f"Verified! Received {value_eth:.6f} ETH ✅"

    except Exception as e:
        return False, f"Verification error: {e}"

# ── Census lookup ─────────────────────────────────────────────────────────────
def get_median_income(zip_code: str) -> str:
    params = {
        "get": "B19013_001E,NAME",
        "for": f"zip code tabulation area:{zip_code}",
    }
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
    income = int(income_raw)
    return (
        f"📍 *{name}*\n"
        f"💰 Median Household Income: *${income:,.0f}*\n"
        f"_(Source: US Census ACS 5-Year Estimates, 2022)_"
    )

# ── Handlers ──────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if is_subscribed(user_id):
        expiry = get_expiry(user_id)
        await update.message.reply_text(
            f"✅ You have an active subscription until *{expiry}*.\n\n"
            "Send any 5-digit ZIP code to look up median income!",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "👋 Welcome to *ZIP Income Bot*!\n\n"
            "🔒 This bot requires a *$1 / 7-day subscription* paid in ETH.\n\n"
            "Use /pay to get started.",
            parse_mode="Markdown"
        )

async def pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if is_subscribed(user_id):
        expiry = get_expiry(user_id)
        await update.message.reply_text(f"✅ You're already subscribed until *{expiry}*.", parse_mode="Markdown")
        return

    eth_amount = usd_to_eth(PRICE_USD)
    context.user_data["expected_eth"] = eth_amount

    await update.message.reply_text(
        f"💳 *Payment Instructions*\n\n"
        f"Send exactly:\n"
        f"```\n{eth_amount:.6f} ETH\n```\n"
        f"To this wallet:\n"
        f"```\n{YOUR_ETH_WALLET}\n```\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"After sending, reply with your *transaction hash* (0x...) to verify payment.\n\n"
        f"⏳ ETH price updates each time you run /pay",
        parse_mode="Markdown"
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if is_subscribed(user_id):
        expiry = get_expiry(user_id)
        await update.message.reply_text(f"✅ Active subscription until *{expiry}*.", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ No active subscription. Use /pay to subscribe.", parse_mode="Markdown")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    text = update.message.text.strip()

    # Check if it looks like a tx hash
    if text.startswith("0x") and len(text) == 66:
        expected_eth = context.user_data.get("expected_eth")
        if not expected_eth:
            # Recalculate if session lost
            expected_eth = usd_to_eth(PRICE_USD)

        await update.message.reply_text("🔍 Verifying your transaction on Etherscan...")
        ok, msg = verify_tx(text, expected_eth)
        if ok:
            add_subscription(user_id)
            expiry = get_expiry(user_id)
            await update.message.reply_text(
                f"🎉 *Payment confirmed!*\n\n"
                f"{msg}\n\n"
                f"✅ Subscription active until *{expiry}*\n\n"
                f"Now send any 5-digit ZIP code!",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(f"❌ *Verification failed:*\n{msg}", parse_mode="Markdown")
        return

    # ZIP code lookup
    if text.isdigit() and len(text) == 5:
        if not is_subscribed(user_id):
            await update.message.reply_text(
                "🔒 You need a subscription to use this bot.\n\nUse /pay to subscribe for $1 (7 days).",
            )
            return
        await update.message.reply_text("🔍 Looking up...")
        result = get_median_income(text)
        await update.message.reply_text(result, parse_mode="Markdown")
        return

    await update.message.reply_text(
        "Send a 5-digit ZIP code to look up income, or /pay to subscribe.",
    )

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pay", pay))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    logger.info("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
