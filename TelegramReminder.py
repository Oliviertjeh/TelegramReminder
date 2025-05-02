#!/usr/bin/env python3
"""Telegram Reminder Bot — single‑file version.

Features
========
* `/add_reminder <when> <msg>` — stores a reminder (media supported if the bot can write `media/`).
                                 If replying to a message without providing <msg>, uses replied text.
* `/list_reminders`             — list upcoming reminders for the chat.
* `/delete_reminder <id>`       — delete by id.
* `/help`                       — show help.

Times are interpreted in the timezone configured with the `TIMEZONE` env‑var
(default *Europe/Amsterdam*).  If **no explicit time** is in the phrase, the
bot assumes **09:00** in the given date. Reminder includes link to original message if added via reply.

Environment variables required in `.env` (in the same folder as the script):
--------------------------------------------------------------------------
TG_API_ID=<integer>
TG_API_HASH=<string>
TG_BOT_TOKEN=<token>
# optional
ALLOWED_CHATS=<comma‑separated ids or @usernames>
TIMEZONE=<IANA tz name>
"""

from __future__ import annotations
import os, re, sys, json, shutil, asyncio, logging, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import uuid # For unique filenames

# ── third‑party ────────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    from dateutil import parser as du_parser
    from telethon import TelegramClient, events
    from telethon.tl.types import Message, User, Chat, Channel # Added more types for hinting
    from telethon.errors.rpcerrorlist import (
        UserIsBlockedError, ChatWriteForbiddenError, FloodWaitError,
        FileReferenceExpiredError, MessageIdInvalidError, BotMethodInvalidError,
        MediaEmptyError)
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError as e:  # pragma: no cover
    missing = e.name
    print(f"Missing dependency: {missing}.  Run:\n  pip install python-dotenv python-dateutil telethon python-zoneinfo")
    sys.exit(1)

# ── logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)-8s | %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("reminderbot")
logging.getLogger('telethon').setLevel(logging.WARNING)
logging.getLogger('asyncio').setLevel(logging.WARNING)

# ── paths / env ────────────────────────────────────────────────────────────────
ROOT       = Path(__file__).resolve().parent
ENV_PATH   = ROOT / ".env"
MEDIA_DIR  = ROOT / "media"
REM_PATH   = ROOT / "reminders.json"
load_dotenv(ENV_PATH)

API_ID     = os.getenv("TG_API_ID")
API_HASH   = os.getenv("TG_API_HASH")
BOT_TOKEN  = os.getenv("TG_BOT_TOKEN")
CHAT_RAW   = os.getenv("ALLOWED_CHATS", "").strip()
TZ_NAME    = os.getenv("TIMEZONE", "Europe/Amsterdam")
CHECK_SECS = 30

if not (API_ID and API_HASH and BOT_TOKEN): log.critical("TG_API_ID / HASH / TOKEN missing in .env"); sys.exit(1)
if not API_ID.isdigit(): log.critical("TG_API_ID must be numeric"); sys.exit(1)
API_ID = int(API_ID)

try: TZ = ZoneInfo(TZ_NAME); log.info("Timezone set to: %s", TZ_NAME)
except ZoneInfoNotFoundError: log.warning("TZ '%s' not found, fallback to Europe/Amsterdam", TZ_NAME); TZ_NAME="Europe/Amsterdam"; TZ=ZoneInfo(TZ_NAME)
except Exception as e: log.error("Error loading TZ '%s': %s. Fallback.", TZ_NAME, e); TZ_NAME="Europe/Amsterdam"; TZ=ZoneInfo(TZ_NAME)

ALLOWED_CHATS: set[int|str] = {int(x) if x.lstrip("-+").isdigit() else x.lstrip("@").lower() for x in CHAT_RAW.split(',') if x.strip()}
if ALLOWED_CHATS: log.info("Restricting commands to: %s", ALLOWED_CHATS)
else: log.info("No chat restrictions applied.")

MEDIA_ENABLED = False
try:
    MEDIA_DIR.mkdir(exist_ok=True); test_file = MEDIA_DIR / f".write_test_{uuid.uuid4()}"; test_file.touch(); test_file.unlink(); MEDIA_ENABLED = True; log.info("Media dir '%s' writable. Attachments enabled.", MEDIA_DIR.relative_to(ROOT))
except Exception as e: log.warning("Media dir '%s' not writable: %s. Media IGNORED.", MEDIA_DIR.relative_to(ROOT), e)

# ── Telegram client ────────────────────────────────────────────────────────────
client = TelegramClient(None, API_ID, API_HASH); log.info("Using MemorySession")

# ── storage ────────────────────────────────────────────────────────────────────
reminders: list[dict] = []; rem_lock = asyncio.Lock(); next_id = 1

# ── regex & helpers ────────────────────────────────────────────────────────────
CMD_ADD   = re.compile(r"^/(?:add[_ ]?reminder)(?:@\w+)?\s+(.+)", re.I | re.S)
CMD_LIST  = re.compile(r"^/(?:list[_ ]?reminders?)(?:@\w+)?$", re.I)
CMD_DEL   = re.compile(r"^/(?:delete|del)[_ ]?reminder(?:@\w+)?\s+(\d+)$", re.I)
CMD_HELP  = re.compile(r"^/(?:help(?:[_ ]?reminder)?)(?:@\w+)?$", re.I)
CMD_START = re.compile(r"^/start(?:@\w+)?$", re.I)
TIME_RE   = re.compile(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\b")

# ── util ───────────────────────────────────────────────────────────────────────

def parse_dt(text: str) -> datetime | None:
    log.debug("Parsing dt: '%s'", text); naive = None; formats = ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M")
    cleaned_text = text.strip()
    for fmt in formats:
        try: naive = datetime.strptime(cleaned_text, fmt); log.debug("Parsed with strptime '%s'", fmt); break
        except ValueError: continue
    if naive is None:
        log.debug("Trying dateutil...");
        try: naive = du_parser.parse(cleaned_text, parserinfo=du_parser.parserinfo(dayfirst=True), fuzzy=False); log.debug("Parsed with dateutil")
        except (du_parser.ParserError, ValueError, OverflowError) as e: log.debug("Dateutil failed: %s", e); return None
        except Exception as e: log.error("Unexpected dateutil error: %s", e, exc_info=True); return None
    try:
        if naive.tzinfo is None: local = naive.replace(tzinfo=TZ); log.debug("Applied TZ via replace: %s", local)
        else: local = naive.astimezone(TZ); log.debug("Converted aware time: %s", local)
        has_time = TIME_RE.search(text)
        if local.hour == 0 and local.minute == 0 and local.second == 0 and not has_time: log.debug("Applying default 9am"); local = local.replace(hour=9, minute=0, second=0, microsecond=0)
        else: log.debug("Not applying default time")
        utc_dt = local.astimezone(timezone.utc); log.debug("Final UTC: %s", utc_dt); return utc_dt
    except Exception as e: log.error("TZ apply error: %s", e, exc_info=True); return None

async def load_reminders() -> None:
    global reminders, next_id
    async with rem_lock:
        if REM_PATH.exists():
            log.info("Loading reminders from %s", REM_PATH)
            try:
                raw_data = REM_PATH.read_text("utf-8"); loaded_list = json.loads(raw_data or "[]")
                if isinstance(loaded_list, list):
                    valid_reminders = []; required_keys = {'id', 'chat_id', 'time', 'caption', 'user_id'}
                    for item in loaded_list:
                        if isinstance(item, dict) and required_keys.issubset(item.keys()) and \
                           all(isinstance(item.get(k), int) for k in ['id', 'chat_id', 'user_id']) and \
                           isinstance(item.get('time'), str) and isinstance(item.get('caption'), str):
                            valid = True
                            for key, expected_type in [('media_path', (str, type(None))), ('replied_chat_id', (int, type(None))), ('replied_message_id', (int, type(None)))]:
                                if key in item and not isinstance(item.get(key), expected_type): log.warning("Invalid type for opt key '%s' ID %s, skipping.", key, item.get('id')); valid = False; break
                            if valid:
                                try: datetime.fromisoformat(item['time']); valid_reminders.append(item)
                                except ValueError: log.warning("Invalid time ISO format ID %s, skipping.", item.get('id'))
                        else: log.warning("Missing/invalid required keys/types: %s, skipping.", item)
                    reminders = valid_reminders; log.info("Loaded %d valid reminders.", len(reminders))
                else: log.warning("%s not JSON list. Starting fresh.", REM_PATH); reminders = []
            except Exception as e: log.error("Could not read/parse %s: %s", REM_PATH, e, exc_info=True); reminders = []
        else: log.info("%s not found. Starting empty.", REM_PATH); reminders = []
        next_id = max((r.get("id", 0) for r in reminders), default=0) + 1
        log.info("Next reminder ID set to %d", next_id)

async def save_reminders() -> None:
    global reminders
    async with rem_lock: reminders_to_save = list(reminders)
    if not os.access(REM_PATH.parent, os.W_OK): log.error("Cannot write to %s dir – skip save!", REM_PATH.parent); return
    tmp_path = REM_PATH.with_suffix(".tmp"); log.debug("Saving %d reminders to %s", len(reminders_to_save), REM_PATH)
    try:
        tmp_path.write_text(json.dumps(reminders_to_save, indent=2, ensure_ascii=False), "utf-8"); shutil.move(str(tmp_path), str(REM_PATH)); log.info("Reminders saved successfully.")
    except Exception as e: log.error("Failed saving reminders: %s", e, exc_info=True); tmp_path.unlink(missing_ok=True)

async def is_allowed(ev: events.NewMessage.Event) -> bool:
    """Checks if the event originated from an allowed chat/user."""
    if not ALLOWED_CHATS:
        return True # No restrictions

    # Assign variables only if needed (i.e., if restrictions exist)
    chat_id = ev.chat_id
    sender_id = ev.sender_id

    # Check direct IDs first
    if chat_id in ALLOWED_CHATS: return True
    if sender_id in ALLOWED_CHATS: return True

    # Check usernames (case-insensitive)
    try: # Checking chat username requires fetching chat info
        chat = await ev.get_chat()
        uname = getattr(chat, "username", None); log.debug("Checking chat @%s", uname);
        if uname and uname.lower() in ALLOWED_CHATS: return True
    except Exception: pass # Ignore errors fetching chat (e.g., restricted)

    try: # Check sender username
        sender = await ev.get_sender()
        uname = getattr(sender, "username", None); log.debug("Checking sender @%s", uname);
        if uname and uname.lower() in ALLOWED_CHATS: return True
    except Exception: pass # Ignore errors fetching sender

    # If none match, deny
    log.warning("Denied command: user=%s chat=%s (Not in ALLOWED_CHATS)", sender_id, chat_id)
    return False

# ── background loop ────────────────────────────────────────────────────────────
async def ticker():
    await asyncio.sleep(15); log.info("Reminder check ticker started (interval: %ds)", CHECK_SECS)
    while True:
        start_time = time.monotonic(); now = datetime.now(timezone.utc); log.debug("Ticker check at %s", now.isoformat())
        to_del_ids: list[int] = []; reminders_checked = 0;
        async with rem_lock: reminders_snapshot = list(reminders)
        for r_data in reminders_snapshot:
            reminders_checked += 1;
            try:
                reminder_time = datetime.fromisoformat(r_data["time"])
                if reminder_time <= now:
                    log.info("Reminder ID %d due.", r_data["id"]); should_delete = await send_reminder(r_data)
                    if should_delete: to_del_ids.append(r_data["id"])
                    else: log.warning("Send failed temporarily ID %d, will retry.", r_data["id"])
            except ValueError: log.error("Invalid time fmt ID %d ('%s'), removing.", r_data.get('id'), r_data.get('time')); to_del_ids.append(r_data["id"])
            except Exception as e: log.error("Error processing check ID %d: %s", r_data.get('id'), e, exc_info=True)
        if to_del_ids:
            log.info("Removing %d reminder(s): %s", len(to_del_ids), to_del_ids); removed_count = 0
            async with rem_lock: original_len = len(reminders); reminders[:] = [r for r in reminders if r.get("id") not in to_del_ids]; removed_count = original_len - len(reminders)
            if removed_count > 0: await save_reminders()
            else: log.warning("Attempted remove %d IDs but list unchanged.", len(to_del_ids))
        proc_time = time.monotonic() - start_time; sleep_for = max(0.5, CHECK_SECS - proc_time)
        log.debug("Ticker check done (%d checked, %d due, %.2fs). Sleep %.1fs", reminders_checked, len(to_del_ids), proc_time, sleep_for)
        await asyncio.sleep(sleep_for)

# ── send reminder ──────────────────────────────────────────────────────────────
async def send_reminder(r: dict) -> bool:
    """Sends the reminder, handling media and errors. Returns True if reminder should be deleted."""
    chat_id = r["chat_id"]; reminder_id = r["id"]; user_id = r.get("user_id"); caption = r.get("caption", "")
    media_filename = r.get("media_path"); media_full_path = MEDIA_DIR / media_filename if media_filename else None
    replied_chat_id = r.get("replied_chat_id"); replied_message_id = r.get("replied_message_id")
    permanent_fail = False; log.info("Sending reminder ID %d to chat %s", reminder_id, chat_id)
    mention = f"User {user_id}"
    try:
        if user_id: user = await client.get_entity(user_id); name = getattr(user, 'first_name', '') + (' ' + getattr(user, 'last_name', '') if getattr(user, 'last_name', '') else ''); mention = f"[{name.strip() or f'User {user_id}'}](tg://user?id={user_id})"
    except ValueError: log.warning("User %s not found for mention", user_id)
    except Exception as e: log.warning("Failed getting mention user %s: %s", user_id, e)
    text = f"⏰ **Reminder for {mention}:**\n\n{caption}" if caption else f"⏰ Reminder for {mention}"

    reply_link = ""
    if replied_chat_id and replied_message_id:
        try:
            if str(replied_chat_id).startswith("-100"): link_chat_id = str(replied_chat_id)[4:]
            else: link_chat_id = str(abs(replied_chat_id))
            reply_link = f"\n\n🔗 [Original Message](https://t.me/c/{link_chat_id}/{replied_message_id})"
        except Exception as e: log.warning("Failed to create reply link msg %d chat %d: %s", replied_message_id, replied_chat_id, e)
    text += reply_link

    media_sent = False
    if media_full_path and media_full_path.exists():
        log.debug("Attempting send with media: %s", media_full_path)
        try:
            await client.send_file(chat_id, file=media_full_path, caption=text, parse_mode="md", link_preview=False); log.info("Reminder %d sent WITH media.", reminder_id); media_sent = True
            try: log.debug("Deleting sent media file: %s", media_full_path); media_full_path.unlink()
            except OSError as e: log.error("Failed deleting media file %s: %s", media_full_path, e)
            return True
        except FileReferenceExpiredError: log.warning("File ref expired for %s, fallback text.", media_full_path)
        except BotMethodInvalidError: log.error("Media type mismatch %s. Fallback text.", media_full_path); permanent_fail = True
        except Exception as e: log.error("Failed send %d WITH media: %s. Fallback text.", reminder_id, e, exc_info=True)

    if not media_sent:
        log.debug("Sending reminder %d TEXT ONLY.", reminder_id); fallback_note = ""
        if media_filename and not (media_full_path and media_full_path.exists()): fallback_note = "\n\n_(Note: media file missing)_"
        elif media_filename: fallback_note = "\n\n_(Note: failed sending media)_"
        try:
            final_text = text + fallback_note
            await client.send_message(chat_id, final_text, parse_mode="md", link_preview=False); log.info("Reminder %d sent TEXT ONLY.", reminder_id)
            if permanent_fail and media_filename:
                 log.warning("Perm media fail ID %d, text OK. Deleting file.", reminder_id)
                 if media_full_path:
                     try: media_full_path.unlink(missing_ok=True)
                     except OSError as e: log.error("Failed cleanup media file %s for perm fail: %s", media_full_path, e)
            return True

        except (UserIsBlockedError, ChatWriteForbiddenError) as e:
            log.error("Cannot send to chat %s (Blocked/Forbidden): %s. Removing %d.", chat_id, e, reminder_id)
            if media_full_path:
                 try: media_full_path.unlink(missing_ok=True)
                 except OSError as unlink_e: log.error("Failed cleanup media file %s on block/forbidden: %s", media_full_path, unlink_e)
            return True # Permanent failure
        except FloodWaitError as e: log.warning("Flood wait (%ds) sending %d. Will retry.", e.seconds, reminder_id); await asyncio.sleep(e.seconds + 1); return False
        except Exception as e: log.error("Failed send TEXT reminder %d: %s", reminder_id, e, exc_info=True); return False

# ── command handlers ───────────────────────────────────────────────────────────
@client.on(events.NewMessage(pattern=CMD_START))
async def handle_start(ev: events.NewMessage.Event):
    if not await is_allowed(ev): return; await ev.reply("👋 Hi! I'm the reminder bot. Use /help.")

@client.on(events.NewMessage(pattern=CMD_HELP))
async def handle_help(ev: events.NewMessage.Event):
    if not await is_allowed(ev): return
    help_msg = ("🤖 **Reminder Bot**\n\n🔹 `/add <when> <msg>`\n   Sets reminder. Reply to attach media/text & link.\n   *Ex:* `/add tmr 10am Check mail`\n\n🔹 `/list`\n   Shows reminders.\n\n🔹 `/del <id>`\n   Deletes by ID.\n\n" + f"*{TZ_NAME} timezone. 9am default.*")
    await ev.reply(help_msg, parse_mode="md") # Shortened example commands

@client.on(events.NewMessage(pattern=CMD_ADD))
async def handle_add(ev: events.NewMessage.Event):
    if not await is_allowed(ev): return

    match = CMD_ADD.match(ev.raw_text)
    if not match:
        log.warning("CMD_ADD pattern failed despite handler trigger: %s", ev.raw_text)
        return
    tail = match.group(1).strip()

    if not tail: await ev.reply("Usage: `/add <when> [<text>]`"); return

    log.info("Processing /add from user %d in chat %d", ev.sender_id, ev.chat_id)
    tokens = tail.split(); parsed_dt_utc = None; command_caption = ""; parse_idx = -1

    for i in range(1, len(tokens) + 1):
        chunk = " ".join(tokens[:i]); dt = parse_dt(chunk)
        if dt: parse_idx = i; parsed_dt_utc = dt
        elif parse_idx != -1: break
    if parse_idx == -1: await ev.reply("❌ Couldn't understand date/time."); return
    command_caption = " ".join(tokens[parse_idx:]).strip()

    if parsed_dt_utc <= datetime.now(timezone.utc) + timedelta(seconds=CHECK_SECS // 2): await ev.reply("⏳ Time too soon."); return

    reply_msg: Message | None = None; replied_chat_id: int | None = None; replied_message_id: int | None = None
    if ev.is_reply:
        reply_msg = await ev.get_reply_message()
        if reply_msg: replied_chat_id = reply_msg.chat_id; replied_message_id = reply_msg.id; log.debug("Command is reply to msg %d chat %d", replied_message_id, replied_chat_id)

    final_caption = command_caption
    if not final_caption and reply_msg and reply_msg.text: final_caption = reply_msg.text.strip(); log.info("Using replied text as caption.")
    elif not final_caption and not (reply_msg and reply_msg.media): await ev.reply("⚠️ Need text or reply to msg w/ text/media."); return

    media_filename = None; media_source_msg: Message | None = None
    if MEDIA_ENABLED:
        if ev.media and not ev.web_preview: media_source_msg = ev
        elif reply_msg and reply_msg.media and not reply_msg.web_preview: media_source_msg = reply_msg
        if media_source_msg:
            try:
                target_dir = MEDIA_DIR; log.info("Downloading media from msg %d to %s...", media_source_msg.id, target_dir)
                downloaded_path_str = await media_source_msg.download_media(file=target_dir)
                if downloaded_path_str:
                    dl_path = Path(downloaded_path_str)
                    if dl_path.exists() and dl_path.stat().st_size > 0: media_filename = dl_path.name; log.info("Media DL OK: %s", media_filename)
                    else: log.error("DL ok but '%s' missing/empty.", dl_path); dl_path.unlink(missing_ok=True); await ev.reply("⚠️ Failed verify media.")
                else: log.error("download_media no path."); await ev.reply("⚠️ Failed save media.")
            except Exception as e: log.error("Media DL failed: %s", e, exc_info=True); await ev.reply(f"⚠️ Error DL media: {type(e).__name__}"); media_filename = None

    global next_id
    async with rem_lock:
        current_id = next_id; next_id += 1
        reminder_data = { "id": current_id, "chat_id": ev.chat_id, "time": parsed_dt_utc.isoformat(), "caption": final_caption, "user_id": ev.sender_id, "media_path": media_filename, "replied_chat_id": replied_chat_id, "replied_message_id": replied_message_id, }
        reminders.append(reminder_data); log.info("Stored reminder ID %d: %s", current_id, reminder_data)
    await save_reminders()
    local_time_str = parsed_dt_utc.astimezone(TZ).strftime("%d %b %Y at %H:%M %Z"); response = f"✅ Reminder `#{current_id}` set for **{local_time_str}**."
    if media_filename: response += "\n📎 Media attached."
    elif media_source_msg and not media_filename: response += "\n*(Media attach failed)*"
    if replied_message_id: response += "\n🔗 Link to original msg included."
    await ev.reply(response, parse_mode="md")

@client.on(events.NewMessage(pattern=CMD_LIST))
async def handle_list(ev: events.NewMessage.Event):
    if not await is_allowed(ev): return
    async with rem_lock: chat_reminders = [r for r in reminders if r.get('chat_id') == ev.chat_id]
    if not chat_reminders: await ev.reply("📭 No reminders set for this chat."); return
    try: chat_reminders.sort(key=lambda r: datetime.fromisoformat(r.get('time', '')))
    except Exception as e: log.error("Sort error: %s", e); await ev.reply("⚠️ Error sorting.")
    lines = [f"📋 **Upcoming Reminders (TZ: {TZ_NAME}):**\n"]; count = 0; max_show=15
    for r in chat_reminders:
        if count >= max_show: lines.append(f"*... {len(chat_reminders) - count} more.*"); break
        try:
            dt = datetime.fromisoformat(r['time']).astimezone(TZ); time_fmt = dt.strftime("%d %b, %H:%M"); icon = "📎" if r.get('media_path') else "💬"; cap = r.get('caption', '')[:50]
            if len(r.get('caption', '')) > 50: cap += "..."; link_icon = "🔗" if r.get('replied_message_id') else ""
            if not cap and icon == "📎": cap = "(Media only)"
            elif not cap: cap = "(No text)"
            lines.append(f"🔹 `ID: {r['id']}` | {time_fmt}\n   `→` {icon}{link_icon} {cap}")
            count += 1
        except Exception as e: log.error("List format error ID %d: %s", r.get('id'), e); lines.append(f"🔹 `ID: {r.get('id')}` - Error")
    await ev.reply("\n".join(lines), parse_mode="md")

@client.on(events.NewMessage(pattern=CMD_DEL))
async def handle_delete(ev: events.NewMessage.Event):
    if not await is_allowed(ev): return

    match = CMD_DEL.match(ev.raw_text)
    if not match:
        await ev.reply("Usage: `/del <id>`")
        return

    try: rid = int(match.group(1))
    except ValueError: await ev.reply("❌ Invalid ID."); return

    removed = False; media_to_del = None
    async with rem_lock:
        original_len = len(reminders); found_reminder = None
        for r in reminders:
            if r.get('id') == rid and r.get('chat_id') == ev.chat_id: found_reminder = r; break
        if found_reminder: reminders.remove(found_reminder); media_to_del = found_reminder.get("media_path"); removed = True
    if removed:
        log.info("Deleted ID %d chat %d.", rid, ev.chat_id); await save_reminders()
        if media_to_del:
            media_file = MEDIA_DIR / media_to_del;
            try: log.debug("Deleting media file %s", media_file); media_file.unlink(missing_ok=True)
            except OSError as e: log.error("Failed deleting media file %s: %s", media_file, e)
        await ev.reply(f"✅ Reminder `#{rid}` deleted.")
    else: log.warning("Delete fail ID %d chat %d.", rid, ev.chat_id); await ev.reply(f"❌ Reminder `#{rid}` not found.")

# ── main execution ─────────────────────────────────────────────────────────────
async def main():
    log.info("Starting Reminder Bot..."); print_config(); await load_reminders(); ticker_task = None
    try:
        log.info("Connecting..."); await client.start(bot_token=BOT_TOKEN); log.info("Client connected.")
        me = await client.get_me(); log.info("Logged in as: @%s (ID: %d)", me.username, me.id)
        log.info("Starting bg ticker..."); ticker_task = asyncio.create_task(ticker()); ticker_task.set_name("ReminderTicker")
        log.info("Bot running! Ctrl+C to stop."); await client.run_until_disconnected()
    except Exception as e: log.critical("Main execution error: %s", e, exc_info=True)
    finally:
        log.info("Shutting down...");
        if client.is_connected():
             try: await client.disconnect()
             except Exception as dc_e: log.error(f"Disconnect error: {dc_e}")
             else: log.info("Client disconnected.")
        if ticker_task and not ticker_task.done():
            log.info("Cancelling ticker task..."); ticker_task.cancel()
            try: await ticker_task
            except asyncio.CancelledError: log.info("Ticker task cancelled.")
            except Exception as task_e: log.error(f"Task shutdown error: {task_e}")
        log.info("Shutdown complete.")

def print_config():
    print("-" * 60); log.info("CONFIG: API ID Set: %s", bool(API_ID)); log.info("CONFIG: API HASH Set: %s", bool(API_HASH)); log.info("CONFIG: Bot Token Set: %s", bool(BOT_TOKEN)); log.info("CONFIG: Timezone: %s", TZ_NAME); log.info("CONFIG: Allowed Chats: %s", ALLOWED_CHATS or "Any"); log.info("CONFIG: Reminder File: %s", REM_PATH); log.info("CONFIG: Media Dir: %s (Enabled: %s)", MEDIA_DIR, MEDIA_ENABLED); log.info("CONFIG: Check Interval: %ds", CHECK_SECS); print("-" * 60)

if __name__ == "__main__":
    if not os.access(ROOT, os.W_OK): log.warning("Script dir '%s' may not be writable.", ROOT)
    try: asyncio.run(main())
    except KeyboardInterrupt: log.info("Ctrl+C received, stopping.")
    except Exception as e: log.critical("Unhandled exception: %s", e, exc_info=True); sys.exit(1)
    sys.exit(0)
