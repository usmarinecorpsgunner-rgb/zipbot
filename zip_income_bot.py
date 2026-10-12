#!/usr/bin/env python3
"""
ZIP Income Bot — Full featured with MongoDB storage.
All data persists in MongoDB Atlas — survives any hosting platform.
"""

import os, logging, requests, secrets, string, re
from datetime import datetime, timedelta, timezone
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
CENSUS_API_KEY    = os.getenv("CENSUS_API_KEY", "")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")
ADMIN_USER_ID     = os.getenv("ADMIN_USER_ID", "")
GOOGLE_VISION_KEY = os.getenv("GOOGLE_VISION_KEY", "")
MONGO_URI         = os.getenv("MONGO_URI", "")

WALLETS = {
    "ETH": "0xa00dbAF96a1bC5fa13868E2876B6e8303CeCd11D",
    "LTC": "LPATdHDDiQZRhNUp77h8cELLne7Uoqk33Z",
    "BTC": "bc1qd4ga556dsnu468pejrqj6s25erxcztpawszd6s",
}

PLANS = {
    "1day":  {"days": 1,  "usd": 2.00,  "label": "1 Day — $2"},
    "7day":  {"days": 7,  "usd": 7.00,  "label": "7 Days — $7"},
    "30day": {"days": 30, "usd": 15.00, "label": "30 Days — $15"},
}

WELCOME_KEY_MINUTES = 60
MAX_HISTORY = 20

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── MongoDB ───────────────────────────────────────────────────────────────────
client = MongoClient(MONGO_URI)
db = client["zipbot"]

users_col     = db["users"]
subs_col      = db["subscriptions"]
keys_col      = db["keys"]
payments_col  = db["payments"]
refs_col      = db["referrals"]
bins_col      = db["bins"]
welcomed_col  = db["welcomed"]
welcome_daily = db["welcome_daily"]

def now_utc():
    return datetime.now(timezone.utc)

# ── User tracking ─────────────────────────────────────────────────────────────
def track_user(user_id: str, username: str = "", first_name: str = "") -> bool:
    existing = users_col.find_one({"_id": user_id})
    is_new = existing is None
    users_col.update_one(
        {"_id": user_id},
        {"$set": {
            "username": username,
            "first_name": first_name,
            "last_seen": now_utc().isoformat(),
        }, "$setOnInsert": {"joined": now_utc().isoformat()}},
        upsert=True
    )
    return is_new

def get_all_user_ids() -> list:
    return [u["_id"] for u in users_col.find({}, {"_id": 1})]

# ── Subscriptions ─────────────────────────────────────────────────────────────
def is_admin(user_id: str) -> bool:
    return bool(ADMIN_USER_ID) and str(user_id) == str(ADMIN_USER_ID)

def is_subscribed(user_id: str) -> bool:
    if is_admin(user_id):
        return True
    doc = subs_col.find_one({"_id": user_id})
    if not doc:
        return False
    try:
        expiry = datetime.fromisoformat(doc["expiry"])
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return now_utc() < expiry
    except Exception:
        return False

def add_subscription(user_id: str, days: float):
    doc = subs_col.find_one({"_id": user_id})
    base = now_utc()
    if doc:
        try:
            current = datetime.fromisoformat(doc["expiry"])
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            if current > now_utc():
                base = current
        except Exception:
            pass
    expiry = base + timedelta(days=days)
    subs_col.update_one({"_id": user_id}, {"$set": {"expiry": expiry.isoformat()}}, upsert=True)

def get_expiry(user_id: str) -> str:
    if is_admin(user_id):
        return "∞ (Admin)"
    doc = subs_col.find_one({"_id": user_id})
    if not doc:
        return "No subscription"
    try:
        expiry = datetime.fromisoformat(doc["expiry"])
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if now_utc() > expiry:
            return "Expired"
        return expiry.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return "Unknown"

# ── History ───────────────────────────────────────────────────────────────────
def add_zip_history(user_id: str, zip_code: str):
    doc = users_col.find_one({"_id": user_id}) or {}
    history = doc.get("history", [])
    if zip_code in history:
        history.remove(zip_code)
    history.insert(0, zip_code)
    users_col.update_one({"_id": user_id}, {"$set": {"history": history[:MAX_HISTORY]}})

def get_zip_history(user_id: str) -> list:
    doc = users_col.find_one({"_id": user_id})
    return doc.get("history", []) if doc else []

# ── Welcome key ───────────────────────────────────────────────────────────────
def should_send_welcome_key(user_id: str) -> bool:
    if welcomed_col.find_one({"_id": user_id}):
        return False
    today = now_utc().strftime("%Y-%m-%d")
    doc = welcome_daily.find_one({"_id": today})
    count = doc.get("count", 0) if doc else 0
    return count < 20

def mark_welcomed(user_id: str):
    welcomed_col.update_one({"_id": user_id}, {"$set": {"time": now_utc().isoformat()}}, upsert=True)
    today = now_utc().strftime("%Y-%m-%d")
    welcome_daily.update_one({"_id": today}, {"$inc": {"count": 1}}, upsert=True)

# ── Keys ──────────────────────────────────────────────────────────────────────
def generate_key() -> str:
    return "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(16))

def create_keys(days: float, qty: int) -> list:
    new_keys = []
    for _ in range(qty):
        k = generate_key()
        keys_col.insert_one({"_id": k, "days": days, "used": False, "created": now_utc().isoformat()})
        new_keys.append(k)
    return new_keys

def redeem_key(key: str, user_id: str):
    doc = keys_col.find_one({"_id": key})
    if not doc:
        return None, "invalid"
    if doc.get("used"):
        return None, "used"
    days = doc.get("days", 1)
    keys_col.update_one({"_id": key}, {"$set": {
        "used": True, "redeemed_by": user_id,
        "redeemed_at": now_utc().isoformat()
    }})
    add_subscription(user_id, days)
    return days, "ok"

# ── Referrals ─────────────────────────────────────────────────────────────────
def get_referral_code(user_id: str) -> str:
    doc = refs_col.find_one({"_id": user_id})
    if not doc:
        code = "REF" + secrets.token_hex(4).upper()
        refs_col.insert_one({"_id": user_id, "code": code, "referred": [], "days_earned": 0})
        return code
    return doc["code"]

def get_user_by_ref_code(code: str):
    doc = refs_col.find_one({"code": code.upper()})
    return doc["_id"] if doc else None

def record_referral(referrer_id: str, new_user_id: str):
    refs_col.update_one({"_id": referrer_id}, {"$addToSet": {"referred": new_user_id}})

def process_referral(referrer_id: str, days_purchased: int) -> bool:
    if days_purchased < 3:
        return False
    doc = refs_col.find_one({"_id": referrer_id})
    if not doc:
        return False
    refs_col.update_one({"_id": referrer_id}, {"$inc": {"days_earned": 1}})
    add_subscription(referrer_id, 1)
    return True

# ── Payments ──────────────────────────────────────────────────────────────────
def record_payment(user_id: str, plan_key: str, coin: str, usd: float, tx_hash: str):
    payments_col.insert_one({
        "user_id": user_id, "plan": plan_key, "coin": coin,
        "usd": usd, "tx": tx_hash, "time": now_utc().isoformat()
    })

# ── Bins ──────────────────────────────────────────────────────────────────────
def get_user_bins(user_id: str) -> dict:
    doc = bins_col.find_one({"_id": user_id})
    return doc.get("bins", {}) if doc else {}

def save_user_bin(user_id: str, title: str, items: list):
    bins_col.update_one(
        {"_id": user_id},
        {"$set": {f"bins.{title}": {"items": items, "created": now_utc().isoformat()}}},
        upsert=True
    )

def delete_user_bin(user_id: str, title: str) -> bool:
    doc = bins_col.find_one({"_id": user_id})
    if not doc or title not in doc.get("bins", {}):
        return False
    bins_col.update_one({"_id": user_id}, {"$unset": {f"bins.{title}": ""}})
    return True

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
            return False, "Wrong wallet."
        value_eth = int(tx.get("value", "0x0"), 16) / 1e18
        if value_eth < expected_eth * 0.95:
            return False, f"Amount too low. Got {value_eth:.6f} ETH, need ~{expected_eth:.6f}."
        if not tx.get("blockNumber"):
            return False, "Still pending."
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
            return False, "Still pending."
        received = sum(o.get("value", 0) for o in tx_data.get("outputs", [])
                      if o.get("recipient", "").lower() == wallet) / 1e8
        if received < expected * 0.95:
            return False, f"Amount too low. Got {received:.8f} {coin}."
        return True, f"Verified! {received:.8f} {coin} ✅"
    except Exception as e:
        return False, f"Error: {e}"

# ── Census ────────────────────────────────────────────────────────────────────
def get_zip_data(zip_code: str) -> dict:
    params = {
        "get": "B19013_001E,B01003_001E,B25077_001E,B17001_002E,B23025_005E,NAME",
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
        return dict(zip(data[0], data[1]))
    except Exception as e:
        return {"error": str(e)}

def format_zip_report(zip_code: str, data: dict) -> str:
    if "error" in data:
        return f"❌ ZIP {zip_code}: {data['error']}"
    def fmt(val, prefix=""):
        try:
            v = int(val)
            return f"{prefix}{v:,}" if v >= 0 else "N/A"
        except Exception:
            return "N/A"
    name = data.get("NAME", zip_code)
    try:
        pov_rate = f"{(int(data.get('B17001_002E',0)) / max(int(data.get('B01003_001E',1)),1)) * 100:.1f}%"
    except Exception:
        pov_rate = "N/A"
    return (
        f"📍 *{name}*\n"
        f"💰 Median Income: *{fmt(data.get('B19013_001E'), '$')}*\n"
        f"🏠 Median Home Value: *{fmt(data.get('B25077_001E'), '$')}*\n"
        f"👥 Population: *{fmt(data.get('B01003_001E'))}*\n"
        f"📉 Poverty Rate: *{pov_rate}*\n"
        f"💼 Unemployed: *{fmt(data.get('B23025_005E'))}*\n"
        f"_(US Census ACS 5-Year, 2022)_"
    )

# ── Keyboards ─────────────────────────────────────────────────────────────────
def plan_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 1 Day — $2",    callback_data="plan_1day")],
        [InlineKeyboardButton("📅 7 Days — $7",   callback_data="plan_7day")],
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
    is_new = track_user(user_id, username, first_name)

    ref_code = context.args[0] if context.args else None
    if ref_code and ref_code.startswith("REF"):
        referrer_id = get_user_by_ref_code(ref_code)
        if referrer_id and referrer_id != user_id:
            doc = users_col.find_one({"_id": user_id})
            if not doc or not doc.get("referred_by"):
                users_col.update_one({"_id": user_id}, {"$set": {"referred_by": referrer_id}})
                record_referral(referrer_id, user_id)

    if is_new and should_send_welcome_key(user_id):
        mark_welcomed(user_id)
        add_subscription(user_id, WELCOME_KEY_MINUTES / 1440)
        await update.message.reply_text(
            f"👋 Welcome *{first_name}*! 🎉\n\n"
            f"You've been given a *free 1-hour trial*!\n\n"
            f"Send any ZIP code to get started. Upgrade anytime with /pay",
            parse_mode="Markdown"
        )
        return

    if is_subscribed(user_id):
        await update.message.reply_text(
            f"👋 Welcome back, *{first_name}*!\n\n"
            f"✅ Subscription active until *{get_expiry(user_id)}*\n\n"
            f"Send a ZIP code or use:\n"
            f"/pay /status /history /compare /refer /bins /features /help",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"👋 Hey *{first_name}*, welcome to *ZIP Income Bot*!\n\n"
            f"📊 Get median income, home values, population, poverty rate & more for any US ZIP.\n\n"
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
        "/history — last 20 ZIP lookups\n"
        "/compare 90210 10001 — compare ZIPs\n"
        "/refer — earn free days\n"
        "/redeem KEY — redeem a key\n"
        "/bins — manage your saved lists\n"
        "/features — see all features\n\n"
        "*Data per ZIP:*\n"
        "• Median income, home value, population\n"
        "• Poverty rate, unemployment",
        parse_mode="Markdown",
        reply_markup=plan_keyboard()
    )

async def features_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚡ *ZIP Income Bot — Features*\n\n"
        "📊 *Data per ZIP:*\n"
        "• Median household income\n"
        "• Median home value\n"
        "• Population\n"
        "• Poverty rate\n"
        "• Unemployment count\n\n"
        "🔍 *Lookups:*\n"
        "• Single: `90210`\n"
        "• Bulk: `90210 10001 30301`\n"
        "• Auto-ranks by income\n"
        "• Screenshot — bot reads ZIPs automatically\n\n"
        "📋 *Tools:*\n"
        "• /history — last 20 lookups\n"
        "• /compare — side by side\n"
        "• /bins — save any info as lists\n\n"
        "💳 *Plans:*\n"
        "• 1 Day — $2\n• 7 Days — $7\n• 30 Days — $15\n\n"
        "🤝 /refer — earn free days by referring!",
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
        await update.message.reply_text("🔒 Subscribe to use this.", reply_markup=plan_keyboard())
        return
    h = get_zip_history(user_id)
    if not h:
        await update.message.reply_text("No ZIP history yet!")
        return
    await update.message.reply_text(
        f"🕐 *Last {len(h)} lookups:*\n\n" + "\n".join([f"`{z}`" for z in h]),
        parse_mode="Markdown"
    )

async def compare_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_subscribed(user_id):
        await update.message.reply_text("🔒 Subscribe to use this.", reply_markup=plan_keyboard())
        return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: `/compare 90210 10001`", parse_mode="Markdown")
        return
    zips = [z for z in context.args if z.isdigit() and len(z) == 5][:5]
    if len(zips) < 2:
        await update.message.reply_text("Need at least 2 valid ZIP codes.")
        return
    await update.message.reply_text(f"🔍 Comparing {len(zips)} ZIPs...")
    results, income_map = [], {}
    for z in zips:
        data = get_zip_data(z)
        results.append(format_zip_report(z, data))
        add_zip_history(user_id, z)
        try:
            income_map[z] = int(data.get("B19013_001E", -1))
        except Exception:
            income_map[z] = -1
    await update.message.reply_text("\n\n".join(results), parse_mode="Markdown")
    ranked = sorted([(z, v) for z, v in income_map.items() if v > 0], key=lambda x: x[1], reverse=True)
    if ranked:
        ranking = "\n".join([f"{i+1}. `{z}` — ${v:,}" for i, (z, v) in enumerate(ranked)])
        await update.message.reply_text(f"🏆 *Ranked:*\n\n{ranking}", parse_mode="Markdown")

async def refer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    code = get_referral_code(user_id)
    bot_username = (await context.bot.get_me()).username
    link = f"https://t.me/{bot_username}?start={code}"
    doc = refs_col.find_one({"_id": user_id}) or {}
    await update.message.reply_text(
        f"🤝 *Your Referral Link:*\n\n`{link}`\n\n"
        f"Friend buys 7+ days → you get *+1 free day!*\n\n"
        f"👥 Referred: *{len(doc.get('referred', []))}*\n"
        f"🎁 Days earned: *{doc.get('days_earned', 0)}*",
        parse_mode="Markdown"
    )

async def redeem_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not context.args:
        await update.message.reply_text("Usage: `/redeem YOURKEYHERE`", parse_mode="Markdown")
        return
    key = context.args[0].strip().upper()
    days, status = redeem_key(key, user_id)
    if status == "invalid":
        await update.message.reply_text("❌ Invalid key.")
    elif status == "used":
        await update.message.reply_text("❌ Key already used.")
    else:
        label = f"{int(days * 24)} hours" if days < 1 else f"{int(days)} days"
        await update.message.reply_text(
            f"🎉 *Key redeemed!*\n\n✅ *{label}* granted!\nExpiry: *{get_expiry(user_id)}*\n\nSend any ZIP!",
            parse_mode="Markdown"
        )

async def bins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_subscribed(user_id):
        await update.message.reply_text("🔒 Subscribe to use this.", reply_markup=plan_keyboard())
        return
    bins = get_user_bins(user_id)

    if not context.args:
        if not bins:
            await update.message.reply_text(
                "📦 *Your Bins*\n\nNo saved bins yet!\n\n"
                "`/bins new` — create\n"
                "`/bins list` — list all\n"
                "`/bins view <title>` — view\n"
                "`/bins add <title> <item>` — add item\n"
                "`/bins delete <title>` — delete",
                parse_mode="Markdown"
            )
            return
        lines = [f"📦 *{t}* — {len(d.get('items',[]))} item(s)" for t, d in bins.items()]
        await update.message.reply_text(
            f"📦 *Your Bins ({len(bins)}):*\n\n" + "\n".join(lines) + "\n\n`/bins view <title>` to open",
            parse_mode="Markdown"
        )
        return

    action = context.args[0].lower()

    if action == "new":
        context.user_data["bin_creating"] = True
        context.user_data["bin_title"] = None
        context.user_data["bin_items"] = []
        await update.message.reply_text("📦 *Create a New Bin*\n\nWhat do you want to name it?", parse_mode="Markdown")
        return

    if action == "list":
        if not bins:
            await update.message.reply_text("No bins yet. Use `/bins new`", parse_mode="Markdown")
            return
        lines = [f"📦 *{t}* — {len(d.get('items',[]))} item(s)" for t, d in bins.items()]
        await update.message.reply_text(f"📦 *Your Bins:*\n\n" + "\n".join(lines), parse_mode="Markdown")
        return

    if action == "view":
        if len(context.args) < 2:
            await update.message.reply_text("Usage: `/bins view <title>`", parse_mode="Markdown")
            return
        title = " ".join(context.args[1:])
        if title not in bins:
            await update.message.reply_text(f"❌ No bin named *{title}*.", parse_mode="Markdown")
            return
        items = bins[title].get("items", [])
        numbered = "\n".join([f"{i+1}. {item}" for i, item in enumerate(items)])
        await update.message.reply_text(
            f"📦 *{title}*\n\n{numbered}\n\n"
            f"`/bins add {title} <item>` to add more",
            parse_mode="Markdown"
        )
        return

    if action == "add":
        if len(context.args) < 3:
            await update.message.reply_text("Usage: `/bins add <title> <item>`", parse_mode="Markdown")
            return
        title = context.args[1]
        item = " ".join(context.args[2:])
        if title not in bins:
            await update.message.reply_text(f"❌ No bin named *{title}*.", parse_mode="Markdown")
            return
        items = bins[title].get("items", [])
        items.append(item)
        save_user_bin(user_id, title, items)
        await update.message.reply_text(
            f"✅ Added to *{title}*! Now has {len(items)} item(s).",
            parse_mode="Markdown"
        )
        return

    if action == "delete":
        if len(context.args) < 2:
            await update.message.reply_text("Usage: `/bins delete <title>`", parse_mode="Markdown")
            return
        title = " ".join(context.args[1:])
        if delete_user_bin(user_id, title):
            await update.message.reply_text(f"🗑️ Bin *{title}* deleted.", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"❌ No bin named *{title}*.", parse_mode="Markdown")
        return

    await update.message.reply_text(
        "`/bins new` `/bins list` `/bins view <t>` `/bins add <t> <item>` `/bins delete <t>`",
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
            f"Send exactly:\n```\n{amount} {coin}\n```\nTo:\n```\n{WALLETS[coin]}\n```\n"
            f"Then paste your transaction hash here.",
            parse_mode="Markdown"
        )
    elif data == "pay":
        await query.message.reply_text("💳 *Choose a plan:*", parse_mode="Markdown", reply_markup=plan_keyboard())

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_subscribed(user_id):
        await update.message.reply_text("🔒 Subscribe to use this.", reply_markup=plan_keyboard())
        return
    await update.message.reply_text("📸 Reading your screenshot...")
    try:
        import base64
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        photo_bytes = await file.download_as_bytearray()
        b64 = base64.b64encode(photo_bytes).decode("utf-8")
        resp = requests.post(
            f"https://vision.googleapis.com/v1/images:annotate?key={GOOGLE_VISION_KEY}",
            json={"requests": [{"image": {"content": b64}, "features": [{"type": "TEXT_DETECTION"}]}]},
            timeout=15
        )
        data = resp.json()
        if "error" in data:
            await update.message.reply_text(f"❌ Vision error: {data['error'].get('message')}")
            return
        responses = data.get("responses", [])
        text = responses[0].get("fullTextAnnotation", {}).get("text", "") if responses else ""
        if not text:
            annotations = responses[0].get("textAnnotations", []) if responses else []
            text = annotations[0].get("description", "") if annotations else ""
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")
        return
    zips = list(dict.fromkeys(re.findall(r'\b\d{5}\b', text)))
    if not zips:
        await update.message.reply_text("⚠️ No ZIP codes found.")
        return
    if len(zips) > 20:
        zips = zips[:20]
    await update.message.reply_text(f"✅ Found {len(zips)} ZIP(s): {' '.join(zips)}\n\n🔍 Looking up...")
    results, income_map = [], {}
    for z in zips:
        d = get_zip_data(z)
        results.append(format_zip_report(z, d))
        add_zip_history(user_id, z)
        try:
            income_map[z] = int(d.get("B19013_001E", -1))
        except Exception:
            income_map[z] = -1
    await update.message.reply_text("\n\n".join(results), parse_mode="Markdown")
    if len(zips) > 1:
        ranked = sorted([(z, v) for z, v in income_map.items() if v > 0], key=lambda x: x[1], reverse=True)
        if ranked:
            await update.message.reply_text(
                "🏆 *Ranked:*\n\n" + "\n".join([f"{i+1}. `{z}` — ${v:,}" for i, (z, v) in enumerate(ranked)]),
                parse_mode="Markdown"
            )

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    track_user(user_id, update.effective_user.username or "", update.effective_user.first_name or "")
    text = update.message.text.strip()

    # Bin creation flow
    if context.user_data.get("bin_creating"):
        if context.user_data.get("bin_title") is None:
            context.user_data["bin_title"] = text
            context.user_data["bin_items"] = []
            await update.message.reply_text(
                f"📦 Bin: *{text}*\n\nNow type your first item.\nSend `done` when finished.",
                parse_mode="Markdown"
            )
            return
        if text.lower() == "done":
            title = context.user_data["bin_title"]
            items = context.user_data["bin_items"]
            if not items:
                await update.message.reply_text("❌ No items added. Start over with `/bins new`", parse_mode="Markdown")
            else:
                save_user_bin(user_id, title, items)
                numbered = "\n".join([f"{i+1}. {item}" for i, item in enumerate(items)])
                await update.message.reply_text(
                    f"✅ *Bin saved!*\n\n📦 *{title}*\n\n{numbered}\n\n`/bins view {title}` to view anytime",
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
                f"✅ Item {count}: *{text}*\n\nSend another or type `done` to save.",
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
            doc = users_col.find_one({"_id": user_id}) or {}
            referrer = doc.get("referred_by")
            if referrer and process_referral(referrer, plan["days"]):
                try:
                    await context.bot.send_message(int(referrer), f"🎉 Your referral subscribed! *+1 free day* added!\nExpiry: *{get_expiry(referrer)}*", parse_mode="Markdown")
                except Exception:
                    pass
            await update.message.reply_text(f"🎉 *Confirmed!*\n\n{msg}\n\n✅ Until *{get_expiry(user_id)}*\n\nSend any ZIP!", parse_mode="Markdown")
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
        await update.message.reply_text(f"🔍 Verifying {pending_coin}...")
        ok, msg = verify_btc_ltc_tx(text, pending_coin, pending_amount)
        if ok:
            plan = PLANS[pending_plan]
            add_subscription(user_id, plan["days"])
            record_payment(user_id, pending_plan, pending_coin, plan["usd"], text)
            doc = users_col.find_one({"_id": user_id}) or {}
            referrer = doc.get("referred_by")
            if referrer and process_referral(referrer, plan["days"]):
                try:
                    await context.bot.send_message(int(referrer), f"🎉 Referral subscribed! *+1 day* added!\nExpiry: *{get_expiry(referrer)}*", parse_mode="Markdown")
                except Exception:
                    pass
            await update.message.reply_text(f"🎉 *Confirmed!*\n\n{msg}\n\n✅ Until *{get_expiry(user_id)}*\n\nSend any ZIP!", parse_mode="Markdown")
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
            zips = zips[:20]
        await update.message.reply_text(f"🔍 Looking up {len(zips)} ZIP(s)...")
        results, income_map = [], {}
        for z in zips:
            d = get_zip_data(z)
            results.append(format_zip_report(z, d))
            add_zip_history(user_id, z)
            try:
                income_map[z] = int(d.get("B19013_001E", -1))
            except Exception:
                income_map[z] = -1
        await update.message.reply_text("\n\n".join(results), parse_mode="Markdown")
        if len(zips) > 1:
            ranked = sorted([(z, v) for z, v in income_map.items() if v > 0], key=lambda x: x[1], reverse=True)
            if ranked:
                await update.message.reply_text(
                    "🏆 *Ranked by Income:*\n\n" + "\n".join([f"{i+1}. `{z}` — ${v:,}" for i, (z, v) in enumerate(ranked)]),
                    parse_mode="Markdown"
                )
        return

    # Redeem key by typing it
    if len(text) == 16 and text.isalnum():
        days, status = redeem_key(text.upper(), user_id)
        if status == "invalid":
            await update.message.reply_text("❌ Invalid key.")
        elif status == "used":
            await update.message.reply_text("❌ Key already used.")
        else:
            label = f"{int(days * 24)} hours" if days < 1 else f"{int(days)} days"
            await update.message.reply_text(
                f"🎉 *Key redeemed!*\n\n✅ *{label}* granted!\nExpiry: *{get_expiry(user_id)}*",
                parse_mode="Markdown"
            )
        return

    await update.message.reply_text(
        "Send ZIP codes to look up data.\nExample: `90210 10001`\n\nNeed access? /pay | Have a key? /redeem",
        parse_mode="Markdown"
    )

# ── Admin commands ────────────────────────────────────────────────────────────
async def genkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)):
        await update.message.reply_text("❌ Admin only.")
        return
    days = float(context.args[0]) if context.args else 1
    qty = min(20, int(context.args[1])) if len(context.args) >= 2 else 1
    label = f"{int(days * 24)}hr" if days < 1 else f"{days}day"
    new_keys = create_keys(days, qty)
    key_list = "\n".join([f"`{k}`" for k in new_keys])
    await update.message.reply_text(
        f"🔑 *{qty} key(s) — {label} each:*\n\n{key_list}\n\nNever expire until redeemed.",
        parse_mode="Markdown"
    )

async def listkeys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)):
        await update.message.reply_text("❌ Admin only.")
        return
    unused = list(keys_col.find({"used": False}))
    used = list(keys_col.find({"used": True}))
    msg = f"🔑 *Keys*\n\n✅ Unused ({len(unused)}):\n"
    msg += "\n".join([f"`{k['_id']}` ({k['days']}d)" for k in unused]) or "None"
    msg += f"\n\n❌ Used ({len(used)}):\n"
    msg += "\n".join([f"`{k['_id']}` → {k.get('redeemed_by','?')}" for k in used]) or "None"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)):
        await update.message.reply_text("❌ Admin only.")
        return
    all_users = list(users_col.find({}))
    if not all_users:
        await update.message.reply_text("No users yet.")
        return
    active = 0
    lines = []
    for u in all_users:
        uid = u["_id"]
        sub = subs_col.find_one({"_id": uid})
        expiry_str = "❌ no sub"
        if sub:
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
        uname = f"@{u['username']}" if u.get("username") else f"ID:{uid}"
        last = (u.get("last_seen") or "")[:10]
        lines.append(f"{uname} ({u.get('first_name','')}) — {expiry_str} — {last}")

    header = f"👥 *{len(all_users)} users, {active} active*\n\n"
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
    payments = list(payments_col.find({}))
    if not payments:
        await update.message.reply_text("No payments yet.")
        return
    total = sum(p.get("usd", 0) for p in payments)
    by_coin = {}
    by_plan = {}
    for p in payments:
        by_coin[p.get("coin","?")] = by_coin.get(p.get("coin","?"), 0) + 1
        by_plan[p.get("plan","?")] = by_plan.get(p.get("plan","?"), 0) + 1
    recent = payments[-5:][::-1]
    recent_lines = "\n".join([f"  ${p['usd']} via {p['coin']} ({p['plan']}) — {p['time'][:10]}" for p in recent])
    await update.message.reply_text(
        f"💰 *Revenue Dashboard*\n\n"
        f"💵 Total: *${total:.2f}*\n"
        f"📦 Payments: *{len(payments)}*\n\n"
        f"*By Coin:*\n" + "\n".join([f"  {c}: {n}" for c, n in by_coin.items()]) +
        f"\n\n*By Plan:*\n" + "\n".join([f"  {p}: {n}" for p, n in by_plan.items()]) +
        f"\n\n*Recent:*\n{recent_lines}",
        parse_mode="Markdown"
    )

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/broadcast message`", parse_mode="Markdown")
        return
    message = " ".join(context.args)
    user_ids = get_all_user_ids()
    await update.message.reply_text(f"📢 Sending to {len(user_ids)} users...")
    sent, failed = 0, 0
    for uid in user_ids:
        try:
            await context.bot.send_message(int(uid), f"📢 *Message from Admin:*\n\n{message}", parse_mode="Markdown")
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"✅ Sent: {sent}\n❌ Failed: {failed}")

async def binsearch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/binsearch Crowns`", parse_mode="Markdown")
        return
    keyword = " ".join(context.args).lower()
    all_bins = list(bins_col.find({}))
    matches = []
    for doc in all_bins:
        uid = doc["_id"]
        user = users_col.find_one({"_id": uid}) or {}
        uname = f"@{user['username']}" if user.get("username") else f"ID:{uid}"
        for title, data in doc.get("bins", {}).items():
            if keyword in title.lower():
                matches.append(f"👤 {uname} — 📦 *{title}*\n`{' '.join(str(i) for i in data.get('items',[]))}` ({len(data.get('items',[]))} items)")
    if not matches:
        await update.message.reply_text(f"❌ No bins matching *{keyword}*", parse_mode="Markdown")
        return
    chunk = f"🔍 *\"{keyword}\" — {len(matches)} result(s):*\n\n"
    for m in matches:
        if len(chunk) + len(m) > 3800:
            await update.message.reply_text(chunk, parse_mode="Markdown")
            chunk = ""
        chunk += m + "\n\n"
    if chunk:
        await update.message.reply_text(chunk.strip(), parse_mode="Markdown")

async def binlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)):
        await update.message.reply_text("❌ Admin only.")
        return
    all_docs = list(bins_col.find({}))
    lines = []
    for doc in all_docs:
        uid = doc["_id"]
        user = users_col.find_one({"_id": uid}) or {}
        uname = f"@{user['username']}" if user.get("username") else f"ID:{uid}"
        for title, data in doc.get("bins", {}).items():
            lines.append(f"👤 {uname} — 📦 *{title}* ({len(data.get('items',[]))} items)")
    if not lines:
        await update.message.reply_text("No bins saved yet.")
        return
    chunk = f"📦 *All Bins ({len(lines)}):*\n\n"
    for line in lines:
        if len(chunk) + len(line) > 3800:
            await update.message.reply_text(chunk, parse_mode="Markdown")
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await update.message.reply_text(chunk, parse_mode="Markdown")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pay", pay))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("features", features_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("compare", compare_cmd))
    app.add_handler(CommandHandler("refer", refer_cmd))
    app.add_handler(CommandHandler("redeem", redeem_cmd))
    app.add_handler(CommandHandler("bins", bins_cmd))
    app.add_handler(CommandHandler("binsearch", binsearch_cmd))
    app.add_handler(CommandHandler("binlist", binlist_cmd))
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
