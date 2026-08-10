#!/usr/bin/env python3
"""
Aura Protocol Tracker Bot (LitVM Testnet)
==========================================
Monitors transactions for the 3 Aura Protocol contracts on LitVM testnet
(LiteForge, chain ID 4441) and posts to a Telegram channel/group:

  1. Every 10 minutes (mode=update): the combined transaction total for
     the 3 contracts, with each contract's name + address listed below.
  2. Every 24 hours (mode=daily, run at midnight UTC): how many
     transactions each contract made that day, plus the combined
     daily total.

All messages are sent in English.

This script is designed to run ONCE per invocation (not as a long-lived
process) — meant to be triggered on a schedule by GitHub Actions (or any
other cron). Each run reads/writes a small JSON state file so the daily
counters carry over between runs.

--------------------------------------------------------------------------
REQUIRED CONFIG:
  Set these as environment variables (e.g. GitHub Actions secrets):
    - BOT_TOKEN: the token @BotFather gives you when you create the bot
    - CHAT_ID:   the channel/group ID where the bot should post
                 (for public channels, usually "@your_channel_name";
                 for private ones, a negative numeric ID)
--------------------------------------------------------------------------
DEPENDENCIES:
  pip install python-telegram-bot==21.* requests
--------------------------------------------------------------------------
USAGE:
  python aura_tracker_bot.py --mode update   # 10-min combined-total post
  python aura_tracker_bot.py --mode daily    # 24h summary post
--------------------------------------------------------------------------
NOTE ON THE EXPLORER:
  LitVM testnet's explorer (Caldera/Blockscout) normally exposes:
    GET {EXPLORER_API}/api/v2/addresses/{address}
  with a field like "transactions_count" (string) in the JSON.

  I could not verify the exact field name live (no network access from
  where this script was written). So the bot:
    1) tries several known field names automatically, and
    2) if DEBUG_MODE = True below, prints the raw JSON the first time it
       queries each address, so you can confirm/adjust the field name in
       under a minute.
--------------------------------------------------------------------------
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests
from telegram import Bot

# ============================== CONFIG ===================================

import os

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

# The 3 Aura Protocol contracts on LitVM testnet
CONTRACTS = {
    "Stake/Unstake": "0x0B779FF5855bc4E6937EbFa64aBE7AB8207f09c3",
    "Mint 10 (A)": "0x6bf699fDed8c7edA845D04eaB689eAaCCbB6e9F5",
    "Mint 10 (B)": "0x956bBD4112bBa4a76f995f57262512d510939E2a",
}

EXPLORER_API = "https://liteforge.explorer.caldera.xyz"

POLL_INTERVAL_SECONDS = 600  # 10 minutes

STATE_FILE = Path(__file__).parent / "aura_tracker_state.json"

DEBUG_MODE = True  # set to False once you confirm the field works

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("aura-tracker")

# ============================ EXPLORER CLIENT =============================

# Possible field names depending on the explorer version (Blockscout v2 / v1)
CANDIDATE_FIELDS = ["transactions_count", "transaction_count", "tx_count", "txCount"]


def fetch_tx_count(address: str) -> int:
    """Returns the total transaction count for one address/contract."""
    url = f"{EXPLORER_API}/api/v2/addresses/{address}"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if DEBUG_MODE:
        log.info("DEBUG raw response for %s: %s", address, json.dumps(data)[:800])

    for field in CANDIDATE_FIELDS:
        if field in data and data[field] is not None:
            return int(data[field])

    # Fallback: Etherscan-style v1 API (module=account&action=txlist)
    fallback_url = (
        f"{EXPLORER_API}/api?module=account&action=txlist&address={address}"
        f"&sort=asc"
    )
    resp2 = requests.get(fallback_url, timeout=15)
    resp2.raise_for_status()
    data2 = resp2.json()
    result = data2.get("result", [])
    if isinstance(result, list):
        return len(result)

    raise RuntimeError(
        f"Could not find a transaction count for {address}. "
        f"Check the JSON printed above (DEBUG_MODE=True) and adjust "
        f"CANDIDATE_FIELDS or the API URL."
    )


def fetch_all_counts() -> dict:
    """Returns {label: tx_count} for every tracked contract."""
    counts = {}
    for label, addr in CONTRACTS.items():
        counts[label] = fetch_tx_count(addr)
        log.info("  %s (%s): %s tx", label, addr, counts[label])
    return counts


# =============================== STATE ====================================

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "day_start_counts": {},  # per-contract count at start of the day
        "day_start_date": None,
    }


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# =============================== TELEGRAM ===================================

bot = Bot(token=BOT_TOKEN)


async def send_message(text: str) -> None:
    await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")


# =============================== CORE LOGIC ============================

async def post_periodic_update() -> None:
    """Posts the combined tx total for the 3 contracts, every 10 min."""
    try:
        counts = fetch_all_counts()
    except Exception as e:
        log.error("Error querying the explorer: %s", e)
        return

    total = sum(counts.values())

    lines = [
        "📡 *Aura Protocol — Testnet Transaction Update*",
        "",
        f"*Combined total:* {total:,} tx",
        "",
    ]
    for label, addr in CONTRACTS.items():
        lines.append(f"{label}: `{addr}`")

    await send_message("\n".join(lines))

    # Make sure today's baseline exists (in case this is the very first run)
    state = load_state()
    today = datetime.now(timezone.utc).date().isoformat()
    if state["day_start_date"] != today:
        state["day_start_date"] = today
        state["day_start_counts"] = counts
        save_state(state)


async def send_daily_summary() -> None:
    """Posts each contract's tx count for the last 24h + combined daily total."""
    state = load_state()
    try:
        counts = fetch_all_counts()
    except Exception as e:
        log.error("Error querying the explorer for the daily summary: %s", e)
        return

    day_start = state.get("day_start_counts", {}) or counts
    daily_counts = {
        label: counts[label] - day_start.get(label, counts[label])
        for label in counts
    }
    daily_total = sum(daily_counts.values())

    lines = ["📊 *Aura Protocol — Daily Summary (last 24h)*", ""]
    for label, count in daily_counts.items():
        lines.append(f"• {label}: *{count:,}* tx today")
    lines.append("")
    lines.append(f"*Combined total today:* {daily_total:,} tx")

    await send_message("\n".join(lines))

    # Reset baseline for the new day
    state["day_start_counts"] = counts
    state["day_start_date"] = datetime.now(timezone.utc).date().isoformat()
    save_state(state)


# =============================== MAIN =========================================

async def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Aura Protocol Tracker Bot")
    parser.add_argument(
        "--mode",
        choices=["update", "daily"],
        required=True,
        help="'update' = 10-min combined-total post. 'daily' = 24h summary post.",
    )
    args = parser.parse_args()

    log.info("Running Aura Protocol Tracker Bot (mode=%s)...", args.mode)

    if args.mode == "update":
        await post_periodic_update()
    else:
        await send_daily_summary()


if __name__ == "__main__":
    asyncio.run(main())

