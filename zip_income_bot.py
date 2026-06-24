#!/usr/bin/env python3
"""
ZIP Income Bot — Full featured with:
- Multi-data per ZIP (income, population, home value, poverty, unemployment)
- ZIP history per user
- Bulk ZIP comparison with ranking
- Affiliate system (1 free day per referral who buys 3+ days)
- Referral tracking
- Revenue dashboard
- Auto-DM new users with 1-hour free key (rate limited to prevent abuse)
- ETH/BTC/LTC payment verification

Env vars:
  TELEGRAM_TOKEN, CENSUS_API_KEY, ETHERSCAN_API_KEY, ADMIN_USER_ID, DB_PATH
"""

import os, json, logging, requests, secrets, string, re
from datetime import datetime, timedelta, timezone
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
DB_FILE           = os.getenv("DB_PATH", "/data/subscribers.json")

WALLETS = {
    "ETH": "0xa00dbAF96a1bC5fa13868E2876B6e8303CeCd11D",
    "LTC": "LPATdHDDiQZRhNUp77h8cELLne7Uoqk33Z",
    "BTC": "bc1qd4ga556dsnu468pejrqj6s25erxcztpawszd6s",
}

PLANS = {
    "1day": {"days": 1,  "usd": 2.00,  "label": "1 Day — $2"},
    "7day": {"days": 7,  "usd": 7.00,  "label": "7 Days — $7"},
    "30day": {"days": 30, "usd": 15.00, "label": "30 Days — $15"},
}

WELCOME_KEY_MINUTES = 60  # free key duration for new users
MAX_HISTORY = 20

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)

# ── DB ────────────────────────────────────────────────────────────────────────
def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE) as f:
            return json.load(f)
    return {}

def save_db(db):
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2)

def now_utc():
    return datetime.now(timezone.utc)

def is_admin(user_id: str) -> bool:
    return bool(ADMIN_USER_ID) and str(user_id) == str(ADMIN_USER_ID)

def is_subscribed(user_id: str) -> bool:
    if is_admin(user_id):
        return True
    db = load_db()
    if user_id not in db:
        return False
    try:
        expiry = datetime.fromisoformat(db[user_id]["expiry"])
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return now_utc() < expiry
    except Exception:
        return False

def add_subscription(user_id: str, days: float):
    db = load_db()
    base = now_utc()
    if user_id in db:
        try:
            current = datetime.fromisoformat(db[user_id]["expiry"])
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            if current > now_utc():
                base = current
        except Exception:
            pass
    expiry = base + timedelta(days=days)
    if user_id not in db:
        db[user_id] = {}
    db[user_id]["expiry"] = expiry.isoformat()
    save_db(db)

def get_expiry(user_id: str) -> str:
    if is_admin(user_id):
        return "∞ (Admin)"
    db = load_db()
    if user_id not in db:
        return "No subscription"
    try:
        expiry = datetime.fromisoformat(db[user_id]["expiry"])
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if now_utc() > expiry:
            return "Expired"
        return expiry.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return "Unknown"

def track_user(user_id: str, username: str = "", first_name: str = ""):
    db = load_db()
    users = db.get("_users", {})
    is_new = user_id not in users
    users[user_id] = {
        "username": username,
        "first_name": first_name,
        "last_seen": now_utc().isoformat(),
        "joined": users.get(user_id, {}).get("joined", now_utc().isoformat())
    }
    db["_users"] = users
    save_db(db)
    return is_new

def get_all_user_ids() -> list:
    db = load_db()
    return list(db.get("_users", {}).keys())

def add_zip_history(user_id: str, zip_code: str):
    db = load_db()
    if user_id not in db:
        db[user_id] = {}
    history = db[user_id].get("history", [])
    if zip_code in history:
        history.remove(zip_code)
    history.insert(0, zip_code)
    db[user_id]["history"] = history[:MAX_HISTORY]
    save_db(db)

def get_zip_history(user_id: str) -> list:
    db = load_db()
    return db.get(user_id, {}).get("history", [])

# ── Affiliate system ──────────────────────────────────────────────────────────
def get_referral_code(user_id: str) -> str:
    db = load_db()
    refs = db.get("_referrals", {})
    if user_id not in refs:
        code = "REF" + secrets.token_hex(4).upper()
        refs[user_id] = {"code": code, "referred": [], "days_earned": 0}
        db["_referrals"] = refs
        save_db(db)
    return refs[user_id]["code"]

def get_user_by_ref_code(code: str):
    db = load_db()
    refs = db.get("_referrals", {})
    for uid, data in refs.items():
        if data.get("code") == code.upper():
            return uid
    return None

def process_referral(referrer_id: str, days_purchased: int):
    """Give referrer 1 free day when someone buys 3+ days."""
    if days_purchased < 3:
        return False
    db = load_db()
    refs = db.get("_referrals", {})
    if referrer_id not in refs:
        return False
    refs[referrer_id]["days_earned"] = refs[referrer_id].get("days_earned", 0) + 1
    db["_referrals"] = refs
    save_db(db)
    add_subscription(referrer_id, 1)
    return True

def record_referral(referrer_id: str, new_user_id: str):
    db = load_db()
    refs = db.get("_referrals", {})
    if referrer_id in refs:
        referred = refs[referrer_id].get("referred", [])
        if new_user_id not in referred:
            referred.append(new_user_id)
            refs[referrer_id]["referred"] = referred
            db["_referrals"] = refs
            save_db(db)

# ── Revenue tracking ──────────────────────────────────────────────────────────
def record_payment(user_id: str, plan_key: str, coin: str, usd: float, tx_hash: str):
    db = load_db()
    payments = db.get("_payments", [])
    payments.append({
        "user_id": user_id,
        "plan": plan_key,
        "coin": coin,
        "usd": usd,
        "tx": tx_hash,
        "time": now_utc().isoformat()
    })
    db["_payments"] = payments
    save_db(db)

# ── Welcome key (anti-abuse) ──────────────────────────────────────────────────
def should_send_welcome_key(user_id: str) -> bool:
    """Only send welcome key to genuinely new users — check account age via Telegram isn't possible,
    so we rate-limit: one welcome key per user_id ever, and max 20/day globally."""
    db = load_db()
    welcomed = db.get("_welcomed", {})
    if user_id in welcomed:
        return False
    # Global daily limit
    today = now_utc().strftime("%Y-%m-%d")
    daily = db.get("_welcome_daily", {})
    count = daily.get(today, 0)
    if count >= 20:
        return False
    return True

def mark_welcomed(user_id: str):
    db = load_db()
    welcomed = db.get("_welcomed", {})
    welcomed[user_id] = now_utc().isoformat()
    db["_welcomed"] = welcomed
    today = now_utc().strftime("%Y-%m-%d")
    daily = db.get("_welcome_daily", {})
    daily[today] = daily.get(today, 0) + 1
    db["_welcome_daily"] = daily
    save_db(db)

# ── Crypto helpers ────────────────────────────────────────────────────────────
def get_crypto_price(coin: str) -> float:
    ids = {"ETH": "ethereum", "BTC": "bitcoin", "LTC": "litecoin"}
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": ids[coin], "vs_currencies": "usd"}, timeout=10
        )
        return float(r.json()[ids[coin]]["usd"])
    except Exception:
        return {"ETH": 3000.0, "BTC": 60000.0, "LTC": 80.0}[coin]

def usd_to_crypto(usd: float, coin: str) -> float:
    price = get_crypto_price(coin)
    decimals = 8 if coin in ("BTC", "LTC") else 6
    return round(usd / price, decimals)

# ── TX Verification ───────────────────────────────────────────────────────────
def verify_eth_tx(tx_hash: str, expected_eth: float) -> tuple:
    try:
        r = requests.get("https://api.etherscan.io/api", params={
            "module": "proxy", "action": "eth_getTransactionByHash",
            "txhash": tx_hash, "apikey": ETHERSCAN_API_KEY,
        }, timeout=10)
        tx = r.json().get("result")
        if not tx:
            return False, "Transaction not found."
        if (tx.get("to") or "").lower() != WALLETS["ETH"].lower():
            return False, "Transaction not sent to correct ETH wallet."
        value_eth = int(tx.get("value", "0x0"), 16) / 1e18
        if value_eth < expected_eth * 0.95:
            return False, f"Amount too low. Got {value_eth:.6f} ETH, need ~{expected_eth:.6f} ETH."
        if not tx.get("blockNumber"):
            return False, "Still pending. Wait for confirmation."
        return True, f"Verified! {value_eth:.6f} ETH ✅"
    except Exception as e:
        return False, f"Error: {e}"

def verify_btc_ltc_tx(tx_hash: str, coin: str, expected: float) -> tuple:
    chain = "bitcoin" if coin == "BTC" else "litecoin"
    wallet = WALLETS[coin].lower()
    try:
        r = requests.get(f"https://api.blockchair.com/{chain}/dashboards/transaction/{tx_hash}", timeout=15)
        data = r.json().get("data", {})
        if not data or tx_hash not in data:
            return False, "Transaction not found."
        tx_data = data[tx_hash]
        if not tx_data.get("transaction", {}).get("block_id"):
            return False, "Still pending. Wait for confirmation."
        received = sum(o.get("value", 0) for o in tx_data.get("outputs", [])
                      if o.get("recipient", "").lower() == wallet) / 1e8
        if received < expected * 0.95:
            return False, f"Amount too low. Got {received:.8f} {coin}, need ~{expected:.8f}."
        return True, f"Verified! {received:.8f} {coin} ✅"
    except Exception as e:
        return False, f"Error: {e}"

# ── Census data ───────────────────────────────────────────────────────────────
def get_zip_data(zip_code: str) -> dict:
    """Fetch multiple data points for a ZIP code."""
    variables = {
        "B19013_001E": "median_income",
        "B01003_001E": "population",
        "B25077_001E": "median_home_value",
        "B17001_002E": "poverty_count",
        "B01003_001E": "population",
        "NAME": "name",
    }
    var_string = "B19013_001E,B01003_001E,B25077_001E,B17001_002E,B23025_005E,NAME"
    params = {
        "get": var_string,
        "for": f"zip code tabulation area:{zip_code}",
    }
    if CENSUS_API_KEY:
        params["key"] = CENSUS_API_KEY
    try:
        r = requests.get("https://api.census.gov/data/2022/acs/acs5", params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if len(data) < 2:
            return {"error": f"No data for ZIP {zip_code}"}
        headers = data[0]
        values = data[1]
        result = dict(zip(headers, values))
        return result
    except Exception as e:
        return {"error": str(e)}

def format_zip_report(zip_code: str, data: dict) -> str:
    if "error" in data:
        return f"❌ ZIP {zip_code}: {data['error']}"

    def fmt_int(val, prefix="", suffix=""):
        try:
            v = int(val)
            if v < 0:
                return "N/A"
            return f"{prefix}{v:,}{suffix}"
        except Exception:
            return "N/A"

    name = data.get("NAME", zip_code)
    income = fmt_int(data.get("B19013_001E"), "$")
    population = fmt_int(data.get("B01003_001E"))
    home_value = fmt_int(data.get("B25077_001E"), "$")
    poverty = data.get("B17001_002E", "-1")
    unemployed = data.get("B23025_005E", "-1")

    # Poverty rate
    try:
        pov_rate = (int(poverty) / int(data.get("B01003_001E", 1))) * 100
        poverty_str = f"{pov_rate:.1f}%"
    except Exception:
        poverty_str = "N/A"

    # Unemployment
    try:
        unemp = int(unemployed)
        unemp_str = f"{unemp:,}" if unemp >= 0 else "N/A"
    except Exception:
        unemp_str = "N/A"

    return (
        f"📍 *{name}*\n"
        f"💰 Median Household Income: *{income}*\n"
        f"🏠 Median Home Value: *{home_value}*\n"
        f"👥 Population: *{population}*\n"
        f"📉 Poverty Rate: *{poverty_str}*\n"
        f"💼 Unemployed: *{unemp_str}*\n"
        f"_(US Census ACS 5-Year, 2022)_"
    )

def get_median_income(zip_code: str) -> str:
    data = get_zip_data(zip_code)
    return format_zip_report(zip_code, data)

def get_income_value(zip_code: str) -> int:
    data = get_zip_data(zip_code)
    try:
        return int(data.get("B19013_001E", -1))
    except Exception:
        return -1

# ── Keyboards ─────────────────────────────────────────────────────────────────
def plan_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 1 Day — $2",   callback_data="plan_1day")],
        [InlineKeyboardButton("📅 7 Days — $7",  callback_data="plan_7day")],
        [InlineKeyboardButton("📅 30 Days — $15", callback_data="plan_30day")],
    ])

def coin_keyboard(plan_key: str):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Ξ ETH", callback_data=f"coin_{plan_key}_ETH"),
        InlineKeyboardButton("₿ BTC", callback_data=f"coin_{plan_key}_BTC"),
        InlineKeyboardButton("Ł LTC", callback_data=f"coin_{plan_key}_LTC"),
    ]])

# ── Handlers ──────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    first_name = update.effective_user.first_name or "there"
    username = update.effective_user.username or ""
    
    # Check if new BEFORE tracking
    db = load_db()
    is_new = user_id not in db.get("_users", {}) and user_id not in db.get("_welcomed", {})
    
    track_user(user_id, username, first_name)

    # Handle referral code from deep link: /start REFxxxxxx
    ref_code = context.args[0] if context.args else None
    if ref_code and ref_code.startswith("REF"):
        referrer_id = get_user_by_ref_code(ref_code)
        if referrer_id and referrer_id != user_id:
            db = load_db()
            if db.get(user_id, {}).get("referred_by") is None:
                if user_id not in db:
                    db[user_id] = {}
                db[user_id]["referred_by"] = referrer_id
                save_db(db)
                record_referral(referrer_id, user_id)

    # Auto welcome key for new users (anti-abuse limited)
    if is_new and should_send_welcome_key(user_id):
        mark_welcomed(user_id)
        add_subscription(user_id, WELCOME_KEY_MINUTES / 1440)  # minutes to days
        await update.message.reply_text(
            f"👋 Welcome *{first_name}*! 🎉\n\n"
            f"You've been given a *free 1-hour trial* to try out the bot!\n\n"
            f"Send any ZIP code to get started. Upgrade anytime with /pay",
            parse_mode="Markdown"
        )
        return

    if is_subscribed(user_id):
        expiry = get_expiry(user_id)
        await update.message.reply_text(
            f"👋 Welcome back, *{first_name}*!\n\n"
            f"✅ Subscription active until *{expiry}*\n\n"
            f"*What I can do:*\n"
            f"• Send a ZIP: `90210`\n"
            f"• Bulk ZIPs: `90210 10001 30301`\n"
            f"• /compare — compare ZIPs side by side\n"
            f"• /history — your recent lookups\n"
            f"• /refer — earn free days by referring friends\n\n"
            f"*Commands:*\n/pay /status /history /compare /refer /help",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"👋 Hey *{first_name}*, welcome to *ZIP Income Bot*!\n\n"
            f"📊 Get median income, home values, population, poverty rate & more for any US ZIP code.\n\n"
            f"*Example:* `90210` → Full Beverly Hills data\n"
            f"Bulk: `90210 10001 30301` → Compare multiple ZIPs\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💳 *Plans:*\n"
            f"📅 1 Day — $2\n📅 7 Days — $7\n📅 30 Days — $15\n\n"
            f"Pay with *ETH, BTC, or LTC*\n\n"
            f"🤝 Refer friends & earn free days! /refer",
            parse_mode="Markdown",
            reply_markup=plan_keyboard()
        )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *ZIP Income Bot — Commands*\n\n"
        "/pay — subscribe\n"
        "/status — check expiry\n"
        "/history — your last 20 ZIP lookups\n"
        "/compare 90210 10001 — compare two ZIPs\n"
        "/refer — get your referral link\n"
        "/help — this menu\n\n"
        "*Data shown per ZIP:*\n"
        "• Median household income\n"
        "• Median home value\n"
        "• Population\n"
        "• Poverty rate\n"
        "• Unemployment count\n\n"
        "*Referral program:*\n"
        "Share your link → friend buys 3+ days → you get 1 free day!",
        parse_mode="Markdown",
        reply_markup=plan_keyboard()
    )

async def pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if is_subscribed(user_id):
        await update.message.reply_text(
            f"✅ Subscribed until *{get_expiry(user_id)}*\n\nExtend your subscription:",
            parse_mode="Markdown", reply_markup=plan_keyboard()
        )
        return
    await update.message.reply_text("💳 *Choose a plan:*", parse_mode="Markdown", reply_markup=plan_keyboard())

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if is_subscribed(user_id):
        await update.message.reply_text(f"✅ Active until *{get_expiry(user_id)}*.", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ No active subscription.", reply_markup=plan_keyboard())

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_subscribed(user_id):
        await update.message.reply_text("🔒 Subscribe to use this feature.", reply_markup=plan_keyboard())
        return
    h = get_zip_history(user_id)
    if not h:
        await update.message.reply_text("No ZIP history yet. Send some ZIP codes to get started!")
        return
    await update.message.reply_text(
        f"🕐 *Your last {len(h)} ZIP lookups:*\n\n" + "\n".join([f"`{z}`" for z in h]),
        parse_mode="Markdown"
    )

async def compare_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_subscribed(user_id):
        await update.message.reply_text("🔒 Subscribe to use this feature.", reply_markup=plan_keyboard())
        return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: `/compare 90210 10001`", parse_mode="Markdown")
        return
    zips = [z for z in context.args if z.isdigit() and len(z) == 5][:5]
    if len(zips) < 2:
        await update.message.reply_text("Please provide at least 2 valid ZIP codes.")
        return
    await update.message.reply_text(f"🔍 Comparing {len(zips)} ZIP codes...")
    results = []
    income_map = {}
    for z in zips:
        data = get_zip_data(z)
        results.append(format_zip_report(z, data))
        try:
            income_map[z] = int(data.get("B19013_001E", -1))
        except Exception:
            income_map[z] = -1
        add_zip_history(user_id, z)

    # Rank by income
    ranked = sorted([(z, v) for z, v in income_map.items() if v > 0], key=lambda x: x[1], reverse=True)
    ranking = "\n".join([f"{i+1}. `{z}` — ${v:,}" for i, (z, v) in enumerate(ranked)])

    await update.message.reply_text("\n\n".join(results), parse_mode="Markdown")
    if ranked:
        await update.message.reply_text(
            f"🏆 *Income Ranking (highest to lowest):*\n\n{ranking}",
            parse_mode="Markdown"
        )

async def refer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    code = get_referral_code(user_id)
    bot_username = (await context.bot.get_me()).username
    link = f"https://t.me/{bot_username}?start={code}"
    db = load_db()
    refs = db.get("_referrals", {}).get(user_id, {})
    referred_count = len(refs.get("referred", []))
    days_earned = refs.get("days_earned", 0)
    await update.message.reply_text(
        f"🤝 *Your Referral Link:*\n\n`{link}`\n\n"
        f"Share this link with friends!\n\n"
        f"*How it works:*\n"
        f"• Friend clicks your link & starts the bot\n"
        f"• They buy a *3-day or longer* plan\n"
        f"• You automatically get *+1 free day* added!\n\n"
        f"📊 *Your stats:*\n"
        f"👥 Total referred: *{referred_count}*\n"
        f"🎁 Days earned: *{days_earned}*",
        parse_mode="Markdown"
    )

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("plan_"):
        plan_key = data[5:]
        plan = PLANS.get(plan_key)
        if not plan:
            return
        context.user_data["plan"] = plan_key
        await query.message.reply_text(
            f"📅 *{plan['label']}* selected.\n\nChoose your coin:",
            parse_mode="Markdown", reply_markup=coin_keyboard(plan_key)
        )

    elif data.startswith("coin_"):
        _, plan_key, coin = data.split("_", 2)
        plan = PLANS.get(plan_key)
        if not plan:
            return
        amount = usd_to_crypto(plan["usd"], coin)
        context.user_data["pending_plan"] = plan_key
        context.user_data["pending_coin"] = coin
        context.user_data["pending_amount"] = amount
        await query.message.reply_text(
            f"💳 *Payment Instructions*\n\n"
            f"Plan: *{plan['label']}*\nCoin: *{coin}*\n\n"
            f"Send exactly:\n```\n{amount} {coin}\n```\n"
            f"To:\n```\n{WALLETS[coin]}\n```\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"After sending, paste your *transaction hash* here.",
            parse_mode="Markdown"
        )
    elif data == "pay":
        await query.message.reply_text("💳 *Choose a plan:*", parse_mode="Markdown", reply_markup=plan_keyboard())

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_subscribed(user_id):
        await update.message.reply_text("🔒 Subscribe to use this feature.", reply_markup=plan_keyboard())
        return

    await update.message.reply_text("📸 Reading your screenshot...")

    try:
        import base64
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        photo_bytes = await file.download_as_bytearray()
        b64 = base64.b64encode(photo_bytes).decode("utf-8")

        api_key = os.getenv("GOOGLE_VISION_KEY", "")
        resp = requests.post(
            f"https://vision.googleapis.com/v1/images:annotate?key={api_key}",
            json={"requests": [{"image": {"content": b64}, "features": [{"type": "TEXT_DETECTION"}]}]},
            timeout=15
        )
        data = resp.json()

        # Debug: catch API errors
        if "error" in data:
            await update.message.reply_text(f"❌ Google Vision error: {data['error'].get('message', 'Unknown error')}\n\nMake sure Cloud Vision API is enabled at console.cloud.google.com")
            return

        responses = data.get("responses", [])
        if not responses:
            await update.message.reply_text("⚠️ No response from Vision API.")
            return

        text = responses[0].get("fullTextAnnotation", {}).get("text", "")
        if not text:
            # Try textAnnotations fallback
            annotations = responses[0].get("textAnnotations", [])
            text = annotations[0].get("description", "") if annotations else ""

    except Exception as e:
        await update.message.reply_text(f"❌ Failed to read image: {e}")
        return

    zips = list(dict.fromkeys(re.findall(r'\b\d{5}\b', text)))
    if not zips:
        await update.message.reply_text("⚠️ No ZIP codes found in that image. Make sure ZIPs are clearly visible.")
        return
    if len(zips) > 20:
        await update.message.reply_text(f"⚠️ Found {len(zips)} ZIPs — showing first 20.")
        zips = zips[:20]

    await update.message.reply_text(f"✅ Found {len(zips)} ZIP(s): {' '.join(zips)}\n\n🔍 Looking up...")
    results = []
    income_map = {}
    for z in zips:
        data = get_zip_data(z)
        results.append(format_zip_report(z, data))
        add_zip_history(user_id, z)
        try:
            income_map[z] = int(data.get("B19013_001E", -1))
        except Exception:
            income_map[z] = -1

    await update.message.reply_text("\n\n".join(results), parse_mode="Markdown")
    if len(zips) > 1:
        ranked = sorted([(z, v) for z, v in income_map.items() if v > 0], key=lambda x: x[1], reverse=True)
        if ranked:
            ranking = "\n".join([f"{i+1}. `{z}` — ${v:,}" for i, (z, v) in enumerate(ranked)])
            await update.message.reply_text(f"🏆 *Ranked by Income:*\n\n{ranking}", parse_mode="Markdown")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    track_user(user_id, update.effective_user.username or "", update.effective_user.first_name or "")
    text = update.message.text.strip()

    # Bin creation flow
    if context.user_data.get("bin_creating"):
        # Step 1: set title
        if context.user_data.get("bin_title") is None:
            context.user_data["bin_title"] = text
            context.user_data["bin_items"] = []
            await update.message.reply_text(
                f"📦 Bin name: *{text}*\n\n"
                f"Now type your first item and send it.\n"
                f"Keep adding items one by one.\n\n"
                f"When you're done, send `done` to save.",
                parse_mode="Markdown"
            )
            return
        # Step 2: collect items
        if text.lower() == "done":
            title = context.user_data["bin_title"]
            items = context.user_data["bin_items"]
            if not items:
                await update.message.reply_text("❌ No items added. Bin not saved. Start over with `/bins new`", parse_mode="Markdown")
            else:
                save_user_bin(user_id, title, items)
                numbered = "\n".join([f"{i+1}. {item}" for i, item in enumerate(items)])
                await update.message.reply_text(
                    f"✅ *Bin saved!*\n\n📦 *{title}*\n\n{numbered}\n\n"
                    f"View it anytime: `/bins view {title}`\n"
                    f"Add more: `/bins add {title} <item>`",
                    parse_mode="Markdown"
                )
            context.user_data["bin_creating"] = False
            context.user_data["bin_title"] = None
            context.user_data["bin_items"] = []
            return
        else:
            context.user_data["bin_items"].append(text)
            count = len(context.user_data["bin_items"])
            await update.message.reply_text(
                f"✅ Item {count} added: *{text}*\n\n"
                f"Send another item or type `done` to save.",
                parse_mode="Markdown"
            )
            return

    # ETH tx hash
    if text.startswith("0x") and len(text) == 66:
        pending_plan = context.user_data.get("pending_plan", "7day")
        pending_amount = context.user_data.get("pending_amount") or usd_to_crypto(PLANS[pending_plan]["usd"], "ETH")
        await update.message.reply_text("🔍 Verifying on Etherscan...")
        ok, msg = verify_eth_tx(text, pending_amount)
        if ok:
            plan = PLANS[pending_plan]
            add_subscription(user_id, plan["days"])
            record_payment(user_id, pending_plan, "ETH", plan["usd"], text)
            # Process referral
            db = load_db()
            referrer = db.get(user_id, {}).get("referred_by")
            if referrer and process_referral(referrer, plan["days"]):
                try:
                    ref_info = db.get("_users", {}).get(referrer, {})
                    ref_name = ref_info.get("first_name", "Your referrer")
                    await context.bot.send_message(
                        chat_id=int(referrer),
                        text=f"🎉 Your referral just subscribed! You've earned *+1 free day!*\nNew expiry: *{get_expiry(referrer)}*",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
            await update.message.reply_text(
                f"🎉 *Payment confirmed!*\n\n{msg}\n\n✅ Active until *{get_expiry(user_id)}*\n\nSend any ZIP code!",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(f"❌ *Failed:*\n{msg}", parse_mode="Markdown")
        return

    # BTC/LTC tx hash
    if len(text) == 64 and all(c in "0123456789abcdefABCDEF" for c in text):
        pending_coin = context.user_data.get("pending_coin")
        pending_plan = context.user_data.get("pending_plan", "7day")
        pending_amount = context.user_data.get("pending_amount")
        if not pending_coin or pending_coin not in ("BTC", "LTC"):
            await update.message.reply_text("⚠️ Use /pay first to select plan and coin.")
            return
        if not pending_amount:
            pending_amount = usd_to_crypto(PLANS[pending_plan]["usd"], pending_coin)
        await update.message.reply_text(f"🔍 Verifying {pending_coin} on Blockchair...")
        ok, msg = verify_btc_ltc_tx(text, pending_coin, pending_amount)
        if ok:
            plan = PLANS[pending_plan]
            add_subscription(user_id, plan["days"])
            record_payment(user_id, pending_plan, pending_coin, plan["usd"], text)
            db = load_db()
            referrer = db.get(user_id, {}).get("referred_by")
            if referrer and process_referral(referrer, plan["days"]):
                try:
                    await context.bot.send_message(
                        chat_id=int(referrer),
                        text=f"🎉 Your referral just subscribed! You earned *+1 free day!*\nNew expiry: *{get_expiry(referrer)}*",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
            await update.message.reply_text(
                f"🎉 *Payment confirmed!*\n\n{msg}\n\n✅ Active until *{get_expiry(user_id)}*\n\nSend any ZIP code!",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(f"❌ *Failed:*\n{msg}", parse_mode="Markdown")
        return

    # ZIP lookups
    tokens = [t.strip().strip(",") for t in text.replace(",", " ").replace("\n", " ").split()]
    zips = [t for t in tokens if t.isdigit() and len(t) == 5]
    if zips:
        if not is_subscribed(user_id):
            await update.message.reply_text("🔒 *Access required!*", parse_mode="Markdown", reply_markup=plan_keyboard())
            return
        if len(zips) > 20:
            await update.message.reply_text("⚠️ Max 20 ZIPs. Showing first 20.")
            zips = zips[:20]

        await update.message.reply_text(f"🔍 Looking up {len(zips)} ZIP code(s)...")
        results = []
        income_map = {}
        for z in zips:
            data = get_zip_data(z)
            results.append(format_zip_report(z, data))
            add_zip_history(user_id, z)
            try:
                income_map[z] = int(data.get("B19013_001E", -1))
            except Exception:
                income_map[z] = -1

        await update.message.reply_text("\n\n".join(results), parse_mode="Markdown")

        # Show ranking if bulk
        if len(zips) > 1:
            ranked = sorted([(z, v) for z, v in income_map.items() if v > 0], key=lambda x: x[1], reverse=True)
            if ranked:
                ranking = "\n".join([f"{i+1}. `{z}` — ${v:,}" for i, (z, v) in enumerate(ranked)])
                await update.message.reply_text(f"🏆 *Ranked by Income:*\n\n{ranking}", parse_mode="Markdown")
        return

    # Redeem key
    if len(text) == 16 and text.isalnum():
        db = load_db()
        keys = db.get("_keys", {})
        if text in keys:
            if keys[text].get("used"):
                await update.message.reply_text("❌ Key already used.")
            else:
                days = keys[text].get("days", 1)
                keys[text]["used"] = True
                keys[text]["redeemed_by"] = user_id
                keys[text]["redeemed_at"] = now_utc().isoformat()
                db["_keys"] = keys
                save_db(db)
                add_subscription(user_id, days)
                await update.message.reply_text(
                    f"🎉 *Key redeemed!*\n\n✅ Access until *{get_expiry(user_id)}*\n\nSend any ZIP code!",
                    parse_mode="Markdown"
                )
        else:
            await update.message.reply_text("❌ Invalid key.")
        return

    await update.message.reply_text(
        "Send ZIP codes to look up data.\n\nExample: `90210 10001 30301`\n\nNeed access? /pay\nEarn free days? /refer",
        parse_mode="Markdown"
    )

async def features_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚡ *ZIP Income Bot — Features*\n\n"
        "📊 *Data per ZIP code:*\n"
        "• Median household income\n"
        "• Median home value\n"
        "• Population\n"
        "• Poverty rate\n"
        "• Unemployment count\n\n"
        "🔍 *Lookup options:*\n"
        "• Single ZIP: `90210`\n"
        "• Bulk ZIPs: `90210 10001 30301`\n"
        "• Auto-ranks bulk results highest to lowest income\n"
        "• Send a screenshot — bot reads ZIPs automatically\n\n"
        "📋 *Tools:*\n"
        "• /history — see your last 20 lookups\n"
        "• /compare 90210 10001 — side by side comparison\n"
        "• /bins — save ZIP lists and run them anytime\n\n"
        "💳 *Plans:*\n"
        "• 1 Day — $2\n"
        "• 7 Days — $7\n"
        "• 30 Days — $15\n"
        "• Pay with ETH, BTC, LTC, or USDT\n\n"
        "🤝 *Referral program:*\n"
        "• /refer — get your unique link\n"
        "• Friend buys 3+ days → you get +1 free day!\n\n"
        "🔑 *Have a key?*\n"
        "• Use /redeem to activate it",
        parse_mode="Markdown",
        reply_markup=plan_keyboard()
    )

async def redeem_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        # They passed the key as an argument: /redeem KEYHERE
        key = context.args[0].strip().upper()
        user_id = str(update.effective_user.id)
        db = load_db()
        keys = db.get("_keys", {})
        if key in keys:
            if keys[key].get("used"):
                await update.message.reply_text("❌ That key has already been used.")
            else:
                days = keys[key].get("days", 1)
                keys[key]["used"] = True
                keys[key]["redeemed_by"] = user_id
                keys[key]["redeemed_at"] = now_utc().isoformat()
                db["_keys"] = keys
                save_db(db)
                add_subscription(user_id, days)
                label = f"{int(days * 24)} hours" if days < 1 else f"{int(days)} days"
                await update.message.reply_text(
                    f"🎉 *Key redeemed!*\n\n✅ *{label}* of access granted!\n\nExpiry: *{get_expiry(user_id)}*\n\nSend any ZIP code to get started!",
                    parse_mode="Markdown"
                )
        else:
            await update.message.reply_text("❌ Invalid key. Double check it and try again.")
    else:
        await update.message.reply_text(
            "🔑 *Redeem a Key*\n\n"
            "Use your key like this:\n"
            "`/redeem YOURKEYHERE`\n\n"
            "Or just type your key directly in chat.",
            parse_mode="Markdown"
        )

# ── Bins system ───────────────────────────────────────────────────────────────
def get_user_bins(user_id: str) -> dict:
    db = load_db()
    return db.get(user_id, {}).get("bins", {})

# ── Bins — general info saver ─────────────────────────────────────────────────
def get_user_bins(user_id: str) -> dict:
    db = load_db()
    return db.get(user_id, {}).get("bins", {})

def save_user_bin(user_id: str, title: str, items: list):
    db = load_db()
    if user_id not in db:
        db[user_id] = {}
    bins = db[user_id].get("bins", {})
    bins[title] = {"items": items, "created": now_utc().isoformat()}
    db[user_id]["bins"] = bins
    save_db(db)

def delete_user_bin(user_id: str, title: str) -> bool:
    db = load_db()
    bins = db.get(user_id, {}).get("bins", {})
    if title in bins:
        del bins[title]
        db[user_id]["bins"] = bins
        save_db(db)
        return True
    return False

async def bins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_subscribed(user_id):
        await update.message.reply_text("🔒 Subscribe to use this feature.", reply_markup=plan_keyboard())
        return

    bins = get_user_bins(user_id)

    if not context.args:
        if not bins:
            await update.message.reply_text(
                "📦 *Your Bins*\n\nNo saved bins yet!\n\n"
                "*Commands:*\n"
                "`/bins new` — create a new bin\n"
                "`/bins view <title>` — view a bin\n"
                "`/bins add <title> <item>` — add to existing bin\n"
                "`/bins delete <title>` — delete a bin\n"
                "`/bins list` — see all your bins",
                parse_mode="Markdown"
            )
            return
        lines = [f"📦 *{title}* — {len(data.get('items',[]))} item(s)" for title, data in bins.items()]
        await update.message.reply_text(
            f"📦 *Your Bins ({len(bins)}):*\n\n" + "\n".join(lines) + "\n\n"
            "`/bins view <title>` to open one\n"
            "`/bins new` to create one",
            parse_mode="Markdown"
        )
        return

    action = context.args[0].lower()

    # /bins new — start creation flow
    if action == "new":
        context.user_data["bin_creating"] = True
        context.user_data["bin_title"] = None
        context.user_data["bin_items"] = []
        await update.message.reply_text(
            "📦 *Create a New Bin*\n\nWhat do you want to name this bin?\n\nExample: `Crowns`",
            parse_mode="Markdown"
        )
        return

    # /bins list
    if action == "list":
        if not bins:
            await update.message.reply_text("No bins saved yet. Use `/bins new` to create one.", parse_mode="Markdown")
            return
        lines = [f"📦 *{title}* — {len(data.get('items',[]))} item(s)" for title, data in bins.items()]
        await update.message.reply_text(
            f"📦 *Your Bins ({len(bins)}):*\n\n" + "\n".join(lines),
            parse_mode="Markdown"
        )
        return

    # /bins view <title>
    if action == "view":
        if len(context.args) < 2:
            await update.message.reply_text("Usage: `/bins view Crowns`", parse_mode="Markdown")
            return
        title = " ".join(context.args[1:])
        if title not in bins:
            await update.message.reply_text(f"❌ No bin named *{title}*.", parse_mode="Markdown")
            return
        items = bins[title].get("items", [])
        numbered = "\n".join([f"{i+1}. {item}" for i, item in enumerate(items)])
        await update.message.reply_text(
            f"📦 *{title}*\n\n{numbered}\n\n"
            f"Add more: `/bins add {title} <item>`\n"
            f"Delete: `/bins delete {title}`",
            parse_mode="Markdown"
        )
        return

    # /bins add <title> <item>
    if action == "add":
        if len(context.args) < 3:
            await update.message.reply_text("Usage: `/bins add Crowns 123456`", parse_mode="Markdown")
            return
        title = context.args[1]
        item = " ".join(context.args[2:])
        if title not in bins:
            await update.message.reply_text(f"❌ No bin named *{title}*. Use `/bins new` to create it.", parse_mode="Markdown")
            return
        items = bins[title].get("items", [])
        items.append(item)
        save_user_bin(user_id, title, items)
        await update.message.reply_text(
            f"✅ Added to *{title}*!\n\n"
            f"*{title}* now has {len(items)} item(s).\n"
            f"Add more: `/bins add {title} <item>`\n"
            f"View: `/bins view {title}`",
            parse_mode="Markdown"
        )
        return

    # /bins delete <title>
    if action == "delete":
        if len(context.args) < 2:
            await update.message.reply_text("Usage: `/bins delete Crowns`", parse_mode="Markdown")
            return
        title = " ".join(context.args[1:])
        if delete_user_bin(user_id, title):
            await update.message.reply_text(f"🗑️ Bin *{title}* deleted.", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"❌ No bin named *{title}*.", parse_mode="Markdown")
        return

    await update.message.reply_text(
        "Commands:\n"
        "`/bins new` — create\n"
        "`/bins list` — list all\n"
        "`/bins view <title>` — view\n"
        "`/bins add <title> <item>` — add item\n"
        "`/bins delete <title>` — delete",
        parse_mode="Markdown"
    )


async def binsearch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/binsearch Crowns`", parse_mode="Markdown")
        return
    keyword = " ".join(context.args).lower()
    db = load_db()
    users = db.get("_users", {})
    matches = []
    for uid, user_info in users.items():
        user_bins = db.get(uid, {}).get("bins", {})
        for title, data in user_bins.items():
            if keyword in title.lower():
                uname = f"@{user_info['username']}" if user_info.get("username") else f"ID:{uid}"
                matches.append({
                    "user": uname, "title": title,
                    "zips": data.get("items", data.get("zips", [])),
                    "created": data.get("created", "")[:10]
                })
    if not matches:
        await update.message.reply_text(f"❌ No bins found matching *{keyword}*", parse_mode="Markdown")
        return
    header = f"🔍 *\"{keyword}\" — {len(matches)} result(s):*\n\n"
    chunk = header
    for m in matches:
        line = f"👤 {m['user']} — 📦 *{m['title']}*\nZIPs: `{' '.join(m['zips'])}` ({len(m['zips'])}) — {m['created']}\n\n"
        if len(chunk) + len(line) > 3800:
            await update.message.reply_text(chunk, parse_mode="Markdown")
            chunk = ""
        chunk += line
    if chunk:
        await update.message.reply_text(chunk.strip(), parse_mode="Markdown")

async def binlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)):
        await update.message.reply_text("❌ Admin only.")
        return
    db = load_db()
    users = db.get("_users", {})
    all_bins = []
    for uid, user_info in users.items():
        user_bins = db.get(uid, {}).get("bins", {})
        for title, data in user_bins.items():
            uname = f"@{user_info['username']}" if user_info.get("username") else f"ID:{uid}"
            all_bins.append({
                "user": uname, "title": title,
                "zips": data.get("items", data.get("zips", [])),
                "created": data.get("created", "")[:10]
            })
    if not all_bins:
        await update.message.reply_text("No bins saved by any users yet.")
        return
    header = f"📦 *All Bins ({len(all_bins)} total):*\n\n"
    chunk = header
    for b in all_bins:
        line = f"👤 {b['user']} — 📦 *{b['title']}*\n`{' '.join(b['zips'])}` ({len(b['zips'])} ZIPs) — {b['created']}\n\n"
        if len(chunk) + len(line) > 3800:
            await update.message.reply_text(chunk, parse_mode="Markdown")
            chunk = ""
        chunk += line
    if chunk:
        await update.message.reply_text(chunk.strip(), parse_mode="Markdown")

def generate_key() -> str:
    return "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(16))

async def genkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)):
        await update.message.reply_text("❌ Admin only.")
        return
    days = float(context.args[0]) if context.args else 1
    qty = min(20, int(context.args[1])) if len(context.args) >= 2 else 1
    label = f"{int(days * 24)}hr" if days < 1 else f"{days}day"
    db = load_db()
    keys = db.get("_keys", {})
    new_keys = []
    for _ in range(qty):
        k = generate_key()
        keys[k] = {"days": days, "used": False, "created": now_utc().isoformat()}
        new_keys.append(k)
    db["_keys"] = keys
    save_db(db)
    key_list = "\n".join([f"`{k}`" for k in new_keys])
    await update.message.reply_text(
        f"🔑 *{qty} key(s) — {label} each:*\n\n{key_list}\n\nNever expire until redeemed.",
        parse_mode="Markdown"
    )

async def listkeys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)):
        await update.message.reply_text("❌ Admin only.")
        return
    db = load_db()
    keys = db.get("_keys", {})
    if not keys:
        await update.message.reply_text("No keys yet.")
        return
    unused = [k for k, v in keys.items() if not v.get("used")]
    used = [k for k, v in keys.items() if v.get("used")]
    msg = f"🔑 *Keys*\n\n✅ Unused ({len(unused)}):\n"
    msg += "\n".join([f"`{k}` ({keys[k]['days']}d)" for k in unused]) or "None"
    msg += f"\n\n❌ Used ({len(used)}):\n"
    msg += "\n".join([f"`{k}` → {keys[k].get('redeemed_by','?')}" for k in used]) or "None"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)):
        await update.message.reply_text("❌ Admin only.")
        return
    db = load_db()
    users = db.get("_users", {})
    if not users:
        await update.message.reply_text("No users yet.")
        return
    active = 0
    lines = []
    for uid, info in users.items():
        sub = db.get(uid, {})
        expiry_str = "❌ no sub"
        if sub.get("expiry"):
            try:
                expiry = datetime.fromisoformat(sub["expiry"])
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if now_utc() < expiry:
                    expiry_str = f"✅ {expiry.strftime('%m/%d')}"
                    active += 1
                else:
                    expiry_str = "❌ expired"
            except Exception:
                pass
        uname = f"@{info['username']}" if info.get("username") else f"ID:{uid}"
        last = (info.get("last_seen") or "")[:10]
        lines.append(f"{uname} ({info.get('first_name','')}) — {expiry_str} — {last}")

    header = f"👥 *{len(users)} users, {active} active*\n\n"
    chunk = header
    for line in lines:
        if len(chunk) + len(line) > 3800:
            await update.message.reply_text(chunk, parse_mode="Markdown")
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await update.message.reply_text(chunk, parse_mode="Markdown")

async def revenue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)):
        await update.message.reply_text("❌ Admin only.")
        return
    db = load_db()
    payments = db.get("_payments", [])
    if not payments:
        await update.message.reply_text("No payments recorded yet.")
        return

    total_usd = sum(p.get("usd", 0) for p in payments)
    by_coin = {}
    by_plan = {}
    for p in payments:
        coin = p.get("coin", "?")
        plan = p.get("plan", "?")
        by_coin[coin] = by_coin.get(coin, 0) + 1
        by_plan[plan] = by_plan.get(plan, 0) + 1

    coin_breakdown = "\n".join([f"  {c}: {n} payments" for c, n in by_coin.items()])
    plan_breakdown = "\n".join([f"  {p}: {n} sales" for p, n in by_plan.items()])

    # Last 5 payments
    recent = payments[-5:][::-1]
    recent_lines = "\n".join([
        f"  ${p['usd']} via {p['coin']} ({p['plan']}) — {p['time'][:10]}"
        for p in recent
    ])

    await update.message.reply_text(
        f"💰 *Revenue Dashboard*\n\n"
        f"💵 Total Revenue: *${total_usd:.2f}*\n"
        f"📦 Total Payments: *{len(payments)}*\n\n"
        f"*By Coin:*\n{coin_breakdown}\n\n"
        f"*By Plan:*\n{plan_breakdown}\n\n"
        f"*Recent Payments:*\n{recent_lines}",
        parse_mode="Markdown"
    )

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/broadcast message here`", parse_mode="Markdown")
        return
    message = " ".join(context.args)
    user_ids = get_all_user_ids()
    await update.message.reply_text(f"📢 Sending to {len(user_ids)} users...")
    sent, failed = 0, 0
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
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("compare", compare_cmd))
    app.add_handler(CommandHandler("refer", refer_cmd))
    app.add_handler(CommandHandler("bins", bins_cmd))
    app.add_handler(CommandHandler("binsearch", binsearch_cmd))
    app.add_handler(CommandHandler("binlist", binlist_cmd))
    app.add_handler(CommandHandler("features", features_cmd))
    app.add_handler(CommandHandler("redeem", redeem_cmd))
    app.add_handler(CommandHandler("genkey", genkey))
    app.add_handler(CommandHandler("listkeys", listkeys))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("revenue", revenue_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    logger.info("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
