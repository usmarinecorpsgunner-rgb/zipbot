#!/usr/bin/env python3
"""
Telegram bot that looks up median household income by ZIP code
using the US Census Bureau ACS 5-Year Estimates (free, no key needed for basic use).

Requirements:
    pip install python-telegram-bot requests

Usage:
    1. Create a bot via @BotFather on Telegram and get your token.
    2. Set your token below (or use env var TELEGRAM_TOKEN).
    3. Run: python zip_income_bot.py

Commands:
    /start  - Welcome message
    /income <ZIP> - Look up median household income for a ZIP code
    Or just send a 5-digit ZIP directly.
"""

import os
import logging
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN_HERE")

# Census ACS 5-Year Estimates — variable B19013_001E = Median Household Income
CENSUS_URL = "https://api.census.gov/data/2022/acs/acs5"
# Get a free Census API key at https://api.census.gov/data/key_signup.html (optional but recommended)
CENSUS_API_KEY = os.getenv("CENSUS_API_KEY", "")  # leave blank for limited anonymous access

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ── Census lookup ─────────────────────────────────────────────────────────────
def get_median_income(zip_code: str) -> str:
    """Return a formatted string with median household income for a ZIP code."""
    params = {
        "get": "B19013_001E,NAME",
        "for": f"zip code tabulation area:{zip_code}",
    }
    if CENSUS_API_KEY:
        params["key"] = CENSUS_API_KEY

    try:
        resp = requests.get(CENSUS_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.HTTPError as e:
        return f"❌ Census API error: {e}"
    except Exception as e:
        return f"❌ Request failed: {e}"

    # data[0] = headers, data[1] = values
    if len(data) < 2:
        return f"❌ No data found for ZIP {zip_code}. Make sure it's a valid US ZIP code."

    income_raw = data[1][0]
    name = data[1][1]

    if income_raw in (None, "-666666666", "-999999999"):
        return f"⚠️ Data not available for ZIP {zip_code} ({name})."

    income = int(income_raw)
    formatted = f"${income:,.0f}"

    return (
        f"📍 *{name}*\n"
        f"💰 Median Household Income: *{formatted}*\n"
        f"_(Source: US Census ACS 5-Year Estimates, 2022)_"
    )


# ── Handlers ──────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome! I can look up *median household income* by US ZIP code.\n\n"
        "Just send me a 5-digit ZIP code, or use:\n`/income 83401`",
        parse_mode="Markdown",
    )


async def income_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/income 83401`", parse_mode="Markdown")
        return
    zip_code = context.args[0].strip()
    await handle_zip(update, zip_code)


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.isdigit() and len(text) == 5:
        await handle_zip(update, text)
    else:
        await update.message.reply_text(
            "Please send a valid 5-digit US ZIP code, e.g. `83401`",
            parse_mode="Markdown",
        )


async def handle_zip(update: Update, zip_code: str):
    if not (zip_code.isdigit() and len(zip_code) == 5):
        await update.message.reply_text("❌ Please enter a valid 5-digit ZIP code.")
        return
    await update.message.reply_text("🔍 Looking up...")
    result = get_median_income(zip_code)
    await update.message.reply_text(result, parse_mode="Markdown")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if TELEGRAM_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise ValueError("Set your Telegram bot token via TELEGRAM_TOKEN env var or in the script.")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("income", income_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    logger.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
