#!/usr/bin/env python3
"""
reminder_userbot.py – Telegram reminder helper under a *user* account.

🔄 *2025-04-25*: Switched to `dateutil` + `zoneinfo` for unambiguous CEST/CET parsing,
auto-logging of parse/now comparison, plus the existing interactive setup and
safe session-handling features.
"""
import os
import re
import sys
import tempfile
import shutil
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv, set_key
from dateutil import parser as du_parser
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from sqlite3 import OperationalError
from zoneinfo import ZoneInfo

# ─── CONFIG & ENV ──────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
load_dotenv(ENV_PATH)

API_ID = os.getenv("TG_API_ID")
API_HASH = os.getenv("TG_API_HASH")
CHAT_RAW = os.getenv("ALLOWED_CHATS", "").strip()

# Interactive prompts for missing ENV values -----------------------------------
def prompt_env(var, question, validate=lambda v: bool(v.strip())):
    while True:
        val = input(question).strip()
        if validate(val):
            set_key(str(ENV_PATH), var, val)
            return val
        print("❌ Invalid input; please try again.")

if not API_ID:
    API_ID = prompt_env("TG_API_ID", "Enter your TG_API_ID: ", lambda v: v.isdigit())
if not API_HASH:
    API_HASH = prompt_env("TG_API_HASH", "Enter your TG_API_HASH: ", lambda v: len(v) >= 32)
if not CHAT_RAW:
    print("Restrict reminder commands to specific chats? (IDs or @usernames)")
    CHAT_RAW = input("Comma-separated list or leave blank for all: ").strip()
    if CHAT_RAW:
        set_key(str(ENV_PATH), "ALLOWED_CHATS", CHAT_RAW)

API_ID = int(API_ID)
ALLOWED_CHATS = {
    int(x) if x.lstrip("-+").isdigit() else x.lstrip("@")
    for x in CHAT_RAW.split(',') if x.strip()
}

# ─── SESSION HANDLING ──────────────────────────────────────────────────────────
SESSION_NAME = os.getenv("TG_SESSION", "reminder_session")
SESSION_DIR = ROOT / "sessions"
# Make sure this directory exists and is writable
SESSION_DIR.mkdir(exist_ok=True)
SESSION_PATH = SESSION_DIR / f"{SESSION_NAME}.session"

try:
    exists = SESSION_PATH.exists()
    writable = os.access(SESSION_PATH, os.W_OK) if exists else True
except PermissionError:
    exists, writable = True, False
if exists and not writable:
    print(f"❌ Cannot write session file: {SESSION_PATH}")
    print(f"   Fix: sudo chown $(whoami):$(whoami) {SESSION_PATH}")
    sys.exit(1)

# ─── TELETHON CLIENT ───────────────────────────────────────────────────────────
client = TelegramClient(str(SESSION_PATH), API_ID, API_HASH)

# ─── CONSTANTS ─────────────────────────────────────────────────────────────────
CMD_RE = re.compile(r"^/(?:add[_ ]?reminder)\s+(.+)", re.I | re.S)
TIME_RE = re.compile(r"\d{1,2}:[0-5]\d")
TZ = "Europe/Amsterdam"  # zoneinfo handles CET/CEST

# ─── PARSING HELPER ────────────────────────────────────────────────────────────
def parse_dt(text: str):
    """Parse a day-first date/time string into UTC. Default 07:00 local if no time."""
    TZ_LOCAL = ZoneInfo(TZ)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(TZ_LOCAL)

    try:
        dt_naive = du_parser.parse(text, dayfirst=True)
    except (ValueError, OverflowError):
        print(f"[DEBUG parse_dt] Couldn’t parse {text!r} with dateutil")
        return None

    # Apply timezone if missing
    if dt_naive.tzinfo is None:
        dt_local = dt_naive.replace(tzinfo=TZ_LOCAL)
    else:
        dt_local = dt_naive.astimezone(TZ_LOCAL)

    # Default to 07:00 if no explicit time
    if dt_local.hour == 0 and dt_local.minute == 0 and not TIME_RE.search(text):
        dt_local = dt_local.replace(hour=7, minute=0, second=0, microsecond=0)

    # Convert to UTC for scheduling
    dt_utc = dt_local.astimezone(timezone.utc)

    # Debug logs
    print(f"[DEBUG parse_dt] text={text!r}")
    print(f"   now_local = {now_local.strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"   parsed    = {dt_local.strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"   dt_utc    = {dt_utc.strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"   now_utc   = {now_utc.strftime('%Y-%m-%d %H:%M %Z')}")

    return dt_utc

# ─── SCHEDULING FUNCTION ───────────────────────────────────────────────────────
async def schedule_reminder(ev, when, caption, media_path=None):
    sender = await ev.get_sender()
    mention = f"[{sender.first_name}](tg://user?id={sender.id})"
    text = f"⏰ {mention} {caption}" if caption else f"⏰ {mention}"
    if media_path:
        await client.send_file(ev.chat_id, media_path,
                               caption=text, schedule=when, parse_mode="md")
    else:
        await client.send_message(ev.chat_id, text,
                                  schedule=when, parse_mode="md")
    print(f"[DEBUG scheduled] chat={ev.chat_id} when={when.isoformat()} caption={caption!r}")

# ─── EVENT HANDLER ─────────────────────────────────────────────────────────────
@client.on(events.NewMessage(pattern=CMD_RE))
async def handler(ev):
    print(f"[DEBUG recv] chat={ev.chat_id} msg={ev.raw_text!r}")
    # Check allowed-chats
    if ALLOWED_CHATS:
        uname = (getattr(ev.chat, "username", "") or "").lower()
        if ev.chat_id not in ALLOWED_CHATS and uname not in ALLOWED_CHATS:
            return

    tail = CMD_RE.match(ev.raw_text).group(1).strip()
    if not tail:
        return await ev.reply("⚠️ Usage: `/add reminder <date> [time] <text>`", parse_mode="md")

    # Find date chunk
    tokens = tail.split()
    date_str = None
    caption = ""
    for i in range(1, len(tokens)+1):
        part = " ".join(tokens[:i])
        if parse_dt(part):
            date_str = part
            caption = " ".join(tokens[i:]).strip()
            break

    if not date_str:
        return await ev.reply("❌ Invalid date/time.", parse_mode="md")

    when = parse_dt(date_str)
    # Check if in the past
    now_utc = datetime.now(timezone.utc)
    if when <= now_utc:
        return await ev.reply("⏳ That date/time is in the past!", parse_mode="md")

    # Handle media
    media_path, tmp = None, None
    try:
        src = ev if ev.media else (await ev.get_reply_message() if ev.is_reply else None)
        if src and src.media:
            tmp = tempfile.mkdtemp(prefix="tgrem_")
            media_path = await src.download_media(tmp)

        await schedule_reminder(ev, when, caption, media_path)
        # Confirmation in local time
        local_fmt = when.astimezone(ZoneInfo(TZ)).strftime('%d-%m-%Y %H:%M %Z')
        await ev.reply(f"✅ Scheduled for {local_fmt}", parse_mode="md")
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

# ─── HELP COMMAND ─────────────────────────────────────────────────────────────
@client.on(events.NewMessage(pattern=r"^/help reminder$", flags=re.I))
async def help_handler(ev):
    """Send the available commands help text."""
    help_text = (
        "**Available commands:**
"
        "/add reminder   – create a new reminder
"
        "/list reminders – list active reminders
"
        "/delete reminder <ID> – remove a reminder by its ID"
    )
    await ev.reply(help_text, parse_mode="md")

# ─── RUN ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("✅ Reminder userbot starting…")
    try:
        client.start()
    except Exception as e:
        print("❌ Failed to start session:", e)
        sys.exit(1)

    try:
        client.run_until_disconnected()
    except OperationalError as e:
        print("❌ SQLite error:", e)
    except SessionPasswordNeededError:
        print("🔐 Two-factor enabled: restart to enter your password.")
    except ConnectionError as e:
        print("❌ Connection error:", e)
