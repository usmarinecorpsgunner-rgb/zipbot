#!/usr/bin/env python3
"""
Telegram bot — ZIP code median income lookup with crypto payment gate.

Plans:
  1 day  = $1
  3 days = $3
  7 days = $5

Accepted crypto: ETH, BTC, LTC

Env vars required:
  TELEGRAM_TOKEN    - from @BotFather
  CENSUS_API_KEY    - from api.census.gov
  ETHERSCAN_API_KEY - from etherscan.io (free)
  ADMIN_USER_ID     - your Telegram user ID (gets free access)
"""

import os, json, logging, requests, secrets, string, re
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

WALLETS = {
    "ETH": "0xa00dbAF96a1bC5fa13868E2876B6e8303CeCd11D",
    "LTC": "LPATdHDDiQZRhNUp77h8cELLne7Uoqk33Z",
    "BTC": "bc1qd4ga556dsnu468pejrqj6s25erxcztpawszd6s",
}

PLANS = {
    "1day":  {"days": 1, "usd": 1.00, "label": "1 Day — $1"},
    "3day":  {"days": 3, "usd": 3.00, "label": "3 Days — $3"},
    "7day":  {"days": 7, "usd": 5.00, "label": "7 Days — $5"},
}

DB_FILE = "subscribers.json"

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

def track_user(user_id: str, username: str = "", first_name: str = ""):
    db = load_db()
    users = db.get("_users", {})
    users[user_id] = {
        "username": username,
        "first_name": first_name,
        "last_seen": datetime.utcnow().isoformat()
    }
    db["_users"] = users
    save_db(db)

def get_all_user_ids() -> list:
    db = load_db()
    users = db.get("_users", {})
    return list(users.keys())


    return ADMIN_USER_ID and str(user_id) == str(ADMIN_USER_ID)

def is_subscribed(user_id: str) -> bool:
    if is_admin(user_id):
        return True
    db = load_db()
    if user_id not in db:
        return False
    expiry = datetime.fromisoformat(db[user_id]["expiry"])
    return datetime.utcnow() < expiry

def add_subscription(user_id: str, days: int):
    db = load_db()
    # Extend if already subscribed
    if user_id in db:
        try:
            current = datetime.fromisoformat(db[user_id]["expiry"])
            if current > datetime.utcnow():
                expiry = current + timedelta(days=days)
                db[user_id] = {"expiry": expiry.isoformat()}
                save_db(db)
                return
        except Exception:
            pass
    expiry = datetime.utcnow() + timedelta(days=days)
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

# ── Crypto price helpers ──────────────────────────────────────────────────────
def get_crypto_price(coin: str) -> float:
    ids = {"ETH": "ethereum", "BTC": "bitcoin", "LTC": "litecoin"}
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": ids[coin], "vs_currencies": "usd"},
            timeout=10
        )
        return float(r.json()[ids[coin]]["usd"])
    except Exception:
        defaults = {"ETH": 3000.0, "BTC": 60000.0, "LTC": 80.0}
        return defaults[coin]

def usd_to_crypto(usd: float, coin: str) -> float:
    price = get_crypto_price(coin)
    amount = usd / price
    decimals = 8 if coin in ("BTC", "LTC") else 6
    return round(amount, decimals)

# ── Etherscan ETH verification ────────────────────────────────────────────────
def verify_eth_tx(tx_hash: str, expected_eth: float) -> tuple[bool, str]:
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
        if to_addr != WALLETS["ETH"].lower():
            return False, "Transaction was not sent to the correct ETH wallet."
        value_eth = int(tx.get("value", "0x0"), 16) / 1e18
        if value_eth < expected_eth * 0.95:
            return False, f"Amount too low. Received {value_eth:.6f} ETH, expected ~{expected_eth:.6f} ETH."
        if not tx.get("blockNumber"):
            return False, "Transaction is still pending. Please wait for confirmation and try again."
        return True, f"Verified! Received {value_eth:.6f} ETH ✅"
    except Exception as e:
        return False, f"Verification error: {e}"

# ── BTC/LTC verification via Blockchair ──────────────────────────────────────
def verify_btc_ltc_tx(tx_hash: str, coin: str, expected_amount: float) -> tuple[bool, str]:
    chain = "bitcoin" if coin == "BTC" else "litecoin"
    wallet = WALLETS[coin].lower()
    try:
        r = requests.get(
            f"https://api.blockchair.com/{chain}/dashboards/transaction/{tx_hash}",
            timeout=15
        )
        data = r.json().get("data", {})
        if not data or tx_hash not in data:
            return False, "Transaction not found. Make sure it's confirmed and try again."
        tx_data = data[tx_hash]
        tx = tx_data.get("transaction", {})
        outputs = tx_data.get("outputs", [])

        # Check confirmations
        confirmations = tx.get("block_id", 0)
        if not confirmations or confirmations < 0:
            return False, "Transaction is still pending. Wait for at least 1 confirmation."

        # Check outputs for our wallet
        received = 0
        for out in outputs:
            if out.get("recipient", "").lower() == wallet:
                received += out.get("value", 0)

        received_coin = received / 1e8
        if received_coin < expected_amount * 0.95:
            return False, f"Amount too low. Received {received_coin:.8f} {coin}, expected ~{expected_amount:.8f} {coin}."

        return True, f"Verified! Received {received_coin:.8f} {coin} ✅"
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

# ── Keyboards ─────────────────────────────────────────────────────────────────
def plan_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 1 Day — $1",   callback_data="plan_1day")],
        [InlineKeyboardButton("📅 3 Days — $3",  callback_data="plan_3day")],
        [InlineKeyboardButton("📅 7 Days — $5",  callback_data="plan_7day")],
    ])

def coin_keyboard(plan_key: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Ξ ETH", callback_data=f"coin_{plan_key}_ETH"),
         InlineKeyboardButton("₿ BTC", callback_data=f"coin_{plan_key}_BTC"),
         InlineKeyboardButton("Ł LTC", callback_data=f"coin_{plan_key}_LTC")],
    ])

def pay_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("💳 Subscribe", callback_data="pay")]])

# ── Handlers ──────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    first_name = update.effective_user.first_name or "there"
    username = update.effective_user.username or ""
    track_user(user_id, username, first_name)

    if is_subscribed(user_id):
        expiry = get_expiry(user_id)
        await update.message.reply_text(
            f"👋 Welcome back, *{first_name}*!\n\n"
            f"✅ Subscription active until *{expiry}*\n\n"
            f"Send any 5-digit US ZIP code to look up median household income.\n"
            f"You can also send multiple ZIPs: `90210 10001 30301`\n"
            f"Or send a *screenshot* with ZIP codes and I'll extract them!\n\n"
            f"*Commands:*\n"
            f"• /pay — subscribe or renew\n"
            f"• /status — check your subscription\n"
            f"• /help — show guide",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"👋 Hey *{first_name}*, welcome to *ZIP Income Bot*!\n\n"
            f"📊 *What this bot does:*\n"
            f"Send any US ZIP code and instantly get the median household income — powered by US Census data.\n\n"
            f"*Example:* Send `90210` → Beverly Hills income data\n"
            f"Send a *screenshot* of ZIPs and the bot reads them automatically!\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💳 *Plans:*\n"
            f"📅 1 Day — $1\n"
            f"📅 3 Days — $3\n"
            f"📅 7 Days — $5\n\n"
            f"Pay with *ETH, BTC, or LTC*\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"*Commands:*\n"
            f"• /pay — subscribe\n"
            f"• /status — check subscription\n"
            f"• /help — guide",
            parse_mode="Markdown",
            reply_markup=plan_keyboard()
        )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *ZIP Income Bot — Help Guide*\n\n"
        "*Plans:*\n"
        "📅 1 Day — $1\n"
        "📅 3 Days — $3\n"
        "📅 7 Days — $5\n\n"
        "*Accepted crypto:* ETH, BTC, LTC\n\n"
        "*How to use:*\n"
        "1. Use /pay to pick a plan and coin\n"
        "2. Send the exact crypto amount shown\n"
        "3. Paste your transaction hash to verify\n"
        "4. Start looking up ZIP codes!\n\n"
        "*ZIP lookups:*\n"
        "• Single: `90210`\n"
        "• Bulk: `90210 10001 30301`\n"
        "• Screenshot: send a photo with ZIP codes\n\n"
        "*Other:*\n"
        "• Redeem a free key by typing it in chat\n"
        "• /status — check expiry\n\n"
        "*Data source:* US Census ACS 5-Year Estimates (2022)",
        parse_mode="Markdown",
        reply_markup=plan_keyboard()
    )

async def pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if is_subscribed(user_id):
        expiry = get_expiry(user_id)
        await update.message.reply_text(
            f"✅ You're subscribed until *{expiry}*.\n\nPick a plan to extend:",
            parse_mode="Markdown",
            reply_markup=plan_keyboard()
        )
        return
    await update.message.reply_text(
        "💳 *Choose a plan:*",
        parse_mode="Markdown",
        reply_markup=plan_keyboard()
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if is_subscribed(user_id):
        expiry = get_expiry(user_id)
        await update.message.reply_text(f"✅ Active subscription until *{expiry}*.", parse_mode="Markdown")
    else:
        await update.message.reply_text(
            "❌ No active subscription.",
            reply_markup=plan_keyboard()
        )

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # Plan selected → show coin picker
    if data.startswith("plan_"):
        plan_key = data[5:]
        plan = PLANS.get(plan_key)
        if not plan:
            return
        context.user_data["plan"] = plan_key
        await query.message.reply_text(
            f"📅 *{plan['label']}* selected.\n\nChoose your payment coin:",
            parse_mode="Markdown",
            reply_markup=coin_keyboard(plan_key)
        )

    # Coin selected → show payment instructions
    elif data.startswith("coin_"):
        _, plan_key, coin = data.split("_", 2)
        plan = PLANS.get(plan_key)
        if not plan:
            return
        amount = usd_to_crypto(plan["usd"], coin)
        wallet = WALLETS[coin]
        context.user_data["pending_plan"] = plan_key
        context.user_data["pending_coin"] = coin
        context.user_data["pending_amount"] = amount

        await query.message.reply_text(
            f"💳 *Payment Instructions*\n\n"
            f"Plan: *{plan['label']}*\n"
            f"Coin: *{coin}*\n\n"
            f"Send exactly:\n"
            f"```\n{amount} {coin}\n```\n"
            f"To this wallet:\n"
            f"```\n{wallet}\n```\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"After sending, paste your *transaction hash* here to verify.\n"
            f"⏳ Amount based on live price — run /pay again if you wait too long.",
            parse_mode="Markdown"
        )

    elif data == "pay":
        await query.message.reply_text(
            "💳 *Choose a plan:*",
            parse_mode="Markdown",
            reply_markup=plan_keyboard()
        )

# ── Screenshot ZIP extraction via EasyOCR ────────────────────────────────────
async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if not is_subscribed(user_id):
        await update.message.reply_text(
            "🔒 *Access required!*\n\nPick a plan to get started.",
            parse_mode="Markdown",
            reply_markup=plan_keyboard()
        )
        return

    await update.message.reply_text("📸 Reading your screenshot... (may take a few seconds)")

    try:
        import easyocr
        import io
        from PIL import Image
        import numpy as np

        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        photo_bytes = await file.download_as_bytearray()

        img = Image.open(io.BytesIO(photo_bytes))
        img_array = np.array(img)

        reader = easyocr.Reader(['en'], gpu=False)
        results = reader.readtext(img_array, detail=0)
        full_text = " ".join(results)

    except Exception as e:
        await update.message.reply_text(f"❌ Failed to read image: {e}")
        return

    zips = re.findall(r'\b\d{5}\b', full_text)
    zips = list(dict.fromkeys(zips))  # deduplicate, preserve order

    if not zips:
        await update.message.reply_text("⚠️ No ZIP codes found in that image. Make sure the ZIPs are clearly visible.")
        return

    if len(zips) > 20:
        await update.message.reply_text(f"⚠️ Found {len(zips)} ZIPs — showing first 20.")
        zips = zips[:20]

    await update.message.reply_text(f"✅ Found {len(zips)} ZIP(s): {' '.join(zips)}\n\n🔍 Looking up...")
    results = [get_median_income(z) for z in zips]
    await update.message.reply_text("\n\n".join(results), parse_mode="Markdown")

# ── Text message handler ──────────────────────────────────────────────────────
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    track_user(user_id, update.effective_user.username or "", update.effective_user.first_name or "")
    text = update.message.text.strip()

    # Transaction hash verification
    if text.startswith("0x") and len(text) == 66:
        # ETH tx hash
        pending_coin = context.user_data.get("pending_coin", "ETH")
        pending_amount = context.user_data.get("pending_amount")
        pending_plan = context.user_data.get("pending_plan", "7day")
        if not pending_amount:
            pending_amount = usd_to_crypto(PLANS[pending_plan]["usd"], "ETH")
        await update.message.reply_text("🔍 Verifying on Etherscan...")
        ok, msg = verify_eth_tx(text, pending_amount)
        if ok:
            days = PLANS[pending_plan]["days"]
            add_subscription(user_id, days)
            expiry = get_expiry(user_id)
            await update.message.reply_text(
                f"🎉 *Payment confirmed!*\n\n{msg}\n\n"
                f"✅ Subscription active until *{expiry}*\n\nSend any ZIP code to get started!",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(f"❌ *Verification failed:*\n{msg}", parse_mode="Markdown")
        return

    # BTC/LTC tx hash (64 hex chars)
    if len(text) == 64 and all(c in "0123456789abcdefABCDEF" for c in text):
        pending_coin = context.user_data.get("pending_coin")
        pending_amount = context.user_data.get("pending_amount")
        pending_plan = context.user_data.get("pending_plan", "7day")
        if not pending_coin or pending_coin not in ("BTC", "LTC"):
            await update.message.reply_text("⚠️ Please use /pay first to select your plan and coin, then paste your tx hash.")
            return
        if not pending_amount:
            pending_amount = usd_to_crypto(PLANS[pending_plan]["usd"], pending_coin)
        await update.message.reply_text(f"🔍 Verifying {pending_coin} transaction on Blockchair...")
        ok, msg = verify_btc_ltc_tx(text, pending_coin, pending_amount)
        if ok:
            days = PLANS[pending_plan]["days"]
            add_subscription(user_id, days)
            expiry = get_expiry(user_id)
            await update.message.reply_text(
                f"🎉 *Payment confirmed!*\n\n{msg}\n\n"
                f"✅ Subscription active until *{expiry}*\n\nSend any ZIP code to get started!",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(f"❌ *Verification failed:*\n{msg}", parse_mode="Markdown")
        return

    # ZIP lookup — single or bulk
    tokens = [t.strip().strip(",") for t in text.replace(",", " ").replace("\n", " ").split()]
    zips = [t for t in tokens if t.isdigit() and len(t) == 5]

    if zips:
        if not is_subscribed(user_id):
            await update.message.reply_text(
                "🔒 *Access required!*\n\nPick a plan to get started.",
                parse_mode="Markdown",
                reply_markup=plan_keyboard()
            )
            return
        if len(zips) > 20:
            await update.message.reply_text("⚠️ Max 20 ZIPs at a time. Showing first 20.")
            zips = zips[:20]
        if len(zips) == 1:
            await update.message.reply_text("🔍 Looking up...")
            await update.message.reply_text(get_median_income(zips[0]), parse_mode="Markdown")
        else:
            await update.message.reply_text(f"🔍 Looking up {len(zips)} ZIP codes...")
            results = [get_median_income(z) for z in zips]
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
                days = keys[text].get("days", 1)
                keys[text]["used"] = True
                db["_keys"] = keys
                save_db(db)
                add_subscription(user_id, days)
                expiry = get_expiry(user_id)
                await update.message.reply_text(
                    f"🎉 *Key redeemed!*\n\n✅ Access granted until *{expiry}*\n\nSend any ZIP code to get started!",
                    parse_mode="Markdown"
                )
        else:
            await update.message.reply_text("❌ Invalid key. Check it and try again.")
        return

    await update.message.reply_text(
        "Send one or more 5-digit ZIP codes or a screenshot.\n\nExample: `90210 10001 30301`\n\nNeed access? Use /pay",
        parse_mode="Markdown"
    )

# ── Key generation (admin only) ───────────────────────────────────────────────
def generate_key() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(16))

async def genkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_admin(user_id):
        await update.message.reply_text("❌ Admin only command.")
        return

    days = 1
    if context.args:
        try:
            days = max(1, int(context.args[0]))
        except ValueError:
            pass

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
        k = generate_key()
        keys[k] = {"days": days, "used": False}
        new_keys.append(k)
    db["_keys"] = keys
    save_db(db)

    key_list = "\n".join([f"`{k}`" for k in new_keys])
    await update.message.reply_text(
        f"🔑 *Generated {qty} key(s) — {days} day(s) each:*\n\n{key_list}\n\nEach key is single-use.",
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
    used   = [k for k, v in keys.items() if v.get("used")]
    msg  = f"🔑 *Keys*\n\n✅ Unused ({len(unused)}):\n"
    msg += "\n".join([f"`{k}` ({keys[k]['days']}d)" for k in unused]) or "None"
    msg += f"\n\n❌ Used ({len(used)}):\n"
    msg += "\n".join([f"`{k}`" for k in used]) or "None"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_admin(user_id):
        await update.message.reply_text("❌ Admin only command.")
        return
    db = load_db()
    users = db.get("_users", {})
    if not users:
        await update.message.reply_text("No users yet.")
        return

    lines = []
    active = 0
    for uid, info in users.items():
        sub = db.get(uid, {})
        expiry_str = ""
        is_active = False
        if sub.get("expiry"):
            expiry = datetime.fromisoformat(sub["expiry"])
            if datetime.utcnow() < expiry:
                expiry_str = f"✅ until {expiry.strftime('%m/%d')}"
                is_active = True
            else:
                expiry_str = "❌ expired"
        else:
            expiry_str = "❌ no sub"

        if is_active:
            active += 1

        name = info.get("first_name", "")
        uname = f"@{info['username']}" if info.get("username") else f"ID:{uid}"
        last = info.get("last_seen", "")[:10]
        lines.append(f"{uname} ({name}) — {expiry_str} — last seen {last}")

    header = f"👥 *Users: {len(users)} total, {active} active*\n\n"
    # Split into chunks to avoid Telegram message length limit
    chunk = header
    for line in lines:
        if len(chunk) + len(line) > 3800:
            await update.message.reply_text(chunk, parse_mode="Markdown")
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await update.message.reply_text(chunk, parse_mode="Markdown")


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_admin(user_id):
        await update.message.reply_text("❌ Admin only command.")
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: `/broadcast Your message here`\n\nSends to all users who have ever started the bot.",
            parse_mode="Markdown"
        )
        return

    message = " ".join(context.args)
    user_ids = get_all_user_ids()
    sent, failed = 0, 0

    await update.message.reply_text(f"📢 Sending to {len(user_ids)} users...")

    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=int(uid), text=f"📢 *Message from Admin:*\n\n{message}", parse_mode="Markdown")
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(f"✅ Sent: {sent}\n❌ Failed: {failed}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pay", pay))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("genkey", genkey))
    app.add_handler(CommandHandler("listkeys", listkeys))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    logger.info("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
