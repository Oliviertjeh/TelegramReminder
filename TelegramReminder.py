#!/usr/bin/env python3
import os
import re
import sys
import json
import tempfile
import shutil
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv, set_key
from dateutil import parser as du_parser
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from sqlite3 import OperationalError
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ─── CONFIG & ENV ──────────────────────────────────────────────────────────────
ROOT     = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
load_dotenv(ENV_PATH)

API_ID   = os.getenv("TG_API_ID")
API_HASH = os.getenv("TG_API_HASH")
CHAT_RAW = os.getenv("ALLOWED_CHATS", "").strip()
SESSION_NAME = os.getenv("TG_SESSION", "reminder_session")
DEFAULT_TZ = "Europe/Amsterdam"
TZ_NAME  = os.getenv("TIMEZONE", DEFAULT_TZ)
try:
    TZ = ZoneInfo(TZ_NAME)
    print(f"ℹ️ Using timezone: {TZ_NAME}")
except ZoneInfoNotFoundError:
    print(f"⚠️ Warning: Timezone '{TZ_NAME}' not found. Falling back to '{DEFAULT_TZ}'.")
    TZ_NAME = DEFAULT_TZ; TZ = ZoneInfo(TZ_NAME)
except Exception as e:
    print(f"⚠️ Error loading timezone '{TZ_NAME}': {e}. Falling back to '{DEFAULT_TZ}'.")
    TZ_NAME = DEFAULT_TZ; TZ = ZoneInfo(TZ_NAME)


# Interactive prompts for missing ENV values -----------------------------------
def prompt_env(var, question, validate=lambda v: bool(v.strip())):
    if not ENV_PATH.exists():
        try: ENV_PATH.touch(); print(f"📝 Created .env file at: {ENV_PATH}")
        except OSError as e: print(f"❌ Critical: Could not create .env file: {e}"); sys.exit(1)
    while True:
        val = input(question).strip()
        if validate(val):
            try: set_key(str(ENV_PATH), var, val, quote_mode='never'); print(f"✅ Saved {var}"); return val
            except Exception as e: print(f"❌ Error saving {var}: {e}"); return val
        print("❌ Invalid input.")

if not API_ID: API_ID = prompt_env("TG_API_ID",   "API ID: ",    lambda v: v.isdigit())
if not API_HASH: API_HASH = prompt_env("TG_API_HASH","API Hash: ", lambda v: len(v) >= 32)
if os.getenv("ALLOWED_CHATS") is None:
    print("\nRestrict commands? Enter comma-separated chat IDs/@usernames."); CHAT_RAW = input("Leave blank for all chats: ").strip()
    set_key(str(ENV_PATH), "ALLOWED_CHATS", CHAT_RAW, quote_mode='never'); print(f"✅ Saved ALLOWED_CHATS.")
else: CHAT_RAW = os.getenv("ALLOWED_CHATS", "").strip()

API_ID = int(API_ID)
ALLOWED_CHATS = { int(x) if x.lstrip("-+").isdigit() else x.lstrip("@").lower() for x in CHAT_RAW.split(',') if x.strip() }

# ─── SESSION HANDLING ──────────────────────────────────────────────────────────
SESSION_DIR  = ROOT / "sessions"; SESSION_DIR.mkdir(exist_ok=True)
SESSION_PATH = SESSION_DIR / f"{SESSION_NAME}.session"
try:
    if not os.access(SESSION_DIR, os.W_OK): raise PermissionError(f"Cannot write: {SESSION_DIR}")
    exists = SESSION_PATH.exists(); writable = os.access(SESSION_PATH, os.W_OK) if exists else True
except PermissionError as e: print(f"❌ Permissions error: {e}"); sys.exit(1)
if exists and not writable: print(f"❌ Cannot write: {SESSION_PATH}"); sys.exit(1)

# ─── TELETHON CLIENT ───────────────────────────────────────────────────────────
client = TelegramClient(str(SESSION_PATH), API_ID, API_HASH, system_version="4.16.30-vxCUSTOM")

# ─── CONSTANTS ─────────────────────────────────────────────────────────────────
CMD_ADD  = re.compile(r"^/(?:add[_ ]?reminder)\s+(.+)", re.I | re.S)
CMD_LIST = re.compile(r"^/list(?:[_ ]?reminders)?$", re.I)
CMD_DEL  = re.compile(r"^/(?:delete|del)[_ ]?reminder\s+(\d+)$", re.I)
CMD_HELP = re.compile(r"^/help(?:[_ ]?reminder)?$", re.I)
TIME_RE  = re.compile(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\b")

# ─── STORAGE (Robust Loading) ──────────────────────────────────────────────────
REMINDERS_PATH = ROOT / "reminders.json"; reminders = []
if REMINDERS_PATH.exists():
    print(f"💾 Loading reminders from {REMINDERS_PATH}...")
    try:
        loaded_data = json.loads(REMINDERS_PATH.read_text(encoding='utf-8'))
        if isinstance(loaded_data, list):
            valid_items = [r for r in loaded_data if isinstance(r, dict) and all(k in r for k in ['id', 'chat_id', 'scheduled_id', 'time'])]
            if len(valid_items) != len(loaded_data): print(f"⚠️ Filtered {len(loaded_data) - len(valid_items)} invalid items.")
            reminders = valid_items; print(f"✅ Loaded {len(reminders)} reminders.")
        else: print(f"⚠️ Not a JSON list. Init empty."); reminders = []
    except (json.JSONDecodeError, Exception) as e: print(f"⚠️ Error loading: {e}. Init empty."); reminders = []
else: print(f"ℹ️ Reminders file not found.")
next_id = max([r.get("id", 0) for r in reminders], default=0) + 1
print(f"ℹ️ Next reminder ID: {next_id}")

def save_reminders():
    valid_reminders = [r for r in reminders if isinstance(r, dict) and all(k in r for k in ['id', 'chat_id', 'scheduled_id', 'time'])]
    print(f"💾 Saving {len(valid_reminders)} reminders...")
    try:
        temp_path = REMINDERS_PATH.with_suffix('.tmp')
        with open(temp_path, 'w', encoding='utf-8') as f: json.dump(valid_reminders, f, indent=2, ensure_ascii=False)
        shutil.move(str(temp_path), str(REMINDERS_PATH)); print(f"✅ Reminders saved.")
    except Exception as e: print(f"❌ Error saving: {e}"); temp_path.unlink(missing_ok=True)

# ─── PARSE DATETIME ──────────────────────────────────────────────────────────────
def parse_dt(text: str):
    try:
        parser_info = du_parser.parserinfo(dayfirst=True); dt_naive = du_parser.parse(text, parserinfo=parser_info, fuzzy=False)
        dt_local = dt_naive.replace(tzinfo=TZ) if dt_naive.tzinfo is None else dt_naive.astimezone(TZ)
        if dt_local.hour == 0 and dt_local.minute == 0 and dt_local.second == 0 and not TIME_RE.search(text):
            dt_local = dt_local.replace(hour=9, minute=0, second=0, microsecond=0)
        return dt_local.astimezone(timezone.utc)
    except (du_parser.ParserError, ValueError): return None
    except Exception as e: print(f"❌ PARSE ERROR: {e}"); return None

# ─── SCHEDULE REMINDER (Force Upload via Path) ─────────────────────────────────
async def schedule_reminder(ev, when: datetime, caption: str,
                            media_path: str | None = None,    # ONLY use uploaded path now
                            tmp_dir_to_clean: str | None = None):
    """Schedules the message. Uses media_path for upload."""
    global next_id
    if not isinstance(when, datetime) or when.tzinfo != timezone.utc:
        print(f"❌ Internal Error: schedule_reminder needs UTC datetime"); await ev.reply("❌ Internal error (time)."); return None

    sender = await ev.get_sender(); mention = f"[{sender.first_name or 'User'}](tg://user?id={sender.id})"
    text = f"⏰ {mention}: {caption}" if caption else f"⏰ Reminder for {mention}"

    now = datetime.now(timezone.utc); min_schedule_delay = timedelta(seconds=10)
    if when <= now + min_schedule_delay:
        original_time_str = when.astimezone(TZ).strftime('%Y-%m-%d %H:%M:%S %Z')
        when = now + min_schedule_delay; new_time_str = when.astimezone(TZ).strftime('%Y-%m-%d %H:%M:%S %Z')
        print(f"⚠️ Time adjusted: {original_time_str} -> {new_time_str}")

    msg = None
    try:
        when_local_debug = when.astimezone(TZ).strftime('%Y-%m-%d %H:%M:%S %Z')
        schedule_method = "text only"
        if media_path: schedule_method = f"uploading from '{media_path}'"
        print(f"[DEBUG schedule] Attempting schedule: chat={ev.chat_id}, time={when_local_debug}, method={schedule_method}, text='{text[:100]}...'")

        if media_path:
            print(f"[DEBUG schedule] Calling send_file with file=media_path")
            msg = await client.send_file(ev.chat_id, file=media_path, caption=text, schedule=when, parse_mode="md")
        else:
             print(f"[DEBUG schedule] Calling send_message (no media)")
             msg = await client.send_message(ev.chat_id, text, schedule=when, parse_mode="md")

        if msg: print(f"[DEBUG schedule] API call OK, msg_id={msg.id}.")
        else: raise ValueError("Scheduling API call did not return message object.")

    except Exception as e:
        print(f"❌ Failed to schedule message: {e}"); import traceback; traceback.print_exc()
        await ev.reply(f"❌ Failed to schedule message via API: {type(e).__name__}")
        if tmp_dir_to_clean: shutil.rmtree(tmp_dir_to_clean, ignore_errors=True)
        return None
    finally:
        if tmp_dir_to_clean: print(f"[DEBUG schedule] Cleaning tmp dir: {tmp_dir_to_clean}"); shutil.rmtree(tmp_dir_to_clean, ignore_errors=True)

    # --- Store Reminder ---
    current_id = next_id
    reminder = {
        "id": current_id, "chat_id": ev.chat_id, "scheduled_id": msg.id,
        "time": when.isoformat(), "caption": caption, "user_id": sender.id,
        "media_info": {"method": "upload" if media_path else "none"}
    }
    reminders.append(reminder); save_reminders()
    print(f"✅ Stored reminder locally: id={current_id}, sched_id={msg.id}, method={reminder['media_info']['method']}")
    next_id += 1
    return reminder["id"]

# ─── UTILITY: Check Chat Permission ────────────────────────────────────────────
async def is_allowed(ev):
    if not ALLOWED_CHATS: return True
    chat_id = ev.chat_id
    if chat_id in ALLOWED_CHATS: return True
    try: chat = await ev.get_chat(); uname = getattr(chat, "username", None)
    except Exception as e: print(f"ℹ️ Could not get username for {chat_id}: {e}"); uname = None
    if uname and uname.lower() in ALLOWED_CHATS: return True
    sender = await ev.get_sender(); sender_info = f"@{sender.username}" if sender.username else f"ID {sender.id}"
    print(f"🚫 Denied in chat {chat_id} for {sender_info}."); return False

# ─── HANDLERS ─────────────────────────────────────────────────────────────────
@client.on(events.NewMessage(pattern=CMD_ADD))
async def add_handler(ev):
    if not await is_allowed(ev): return

    print(f"[RECV /add] chat={ev.chat_id}, user={ev.sender_id}, msg='{ev.raw_text[:100]}...'")
    match = CMD_ADD.match(ev.raw_text); tail = match.group(1).strip() if match else ""
    if not tail: await ev.reply("⚠️ Usage: `/add reminder <when> <text>`"); return

    # --- Parse Date/Time and Caption from the command ---
    tokens = tail.split(); parsed_dt_utc = None; command_caption = ""; successful_parse_index = -1
    for i in range(1, len(tokens) + 1):
        potential_date_str = " ".join(tokens[:i])
        temp_dt = parse_dt(potential_date_str)
        if temp_dt: parsed_dt_utc = temp_dt; successful_parse_index = i
        elif successful_parse_index != -1: break
    if successful_parse_index != -1: command_caption = " ".join(tokens[successful_parse_index:]).strip()
    else: parsed_dt_utc = None
    if not parsed_dt_utc: await ev.reply("❌ Couldn't parse date/time."); return
    print(f"[DEBUG add] Parsed: time_utc='{parsed_dt_utc}', command_caption='{command_caption}'")

    # --- Check Time ---
    now = datetime.now(timezone.utc); min_future_delta = timedelta(seconds=5)
    if parsed_dt_utc <= now + min_future_delta: await ev.reply("⏳ Time is past/too soon!"); return

    # --- Media handling & Get Original Text ---
    media_path = None; tmp_dir = None; source_message = None; media_source_info = ""
    media_acquired = False; original_text = None

    if ev.media: source_message = ev; media_source_info = "from command msg"
    elif ev.is_reply:
        reply_msg = await ev.get_reply_message()
        if reply_msg:
             source_message = reply_msg; media_source_info = f"from reply {reply_msg.id}"
             original_text = reply_msg.text; print(f"[DEBUG add] Original text: '{original_text[:100]}...'")
             # No forward check needed now
        else: print("[DEBUG add] Reply message object not found.")
    if source_message: print(f"[DEBUG add] Found source message {media_source_info}")
    else: print("[DEBUG add] No source message identified.")

    # --- Download ---
    if source_message and source_message.media:
         print(f"[DEBUG media] Attempting download {media_source_info}.")
         try:
             tmp_dir_path = Path(tempfile.mkdtemp(prefix="tgrem_media_")); tmp_dir = str(tmp_dir_path)
             print(f"[DEBUG media] Download target: {tmp_dir}")
             dl_path = await source_message.download_media(file=tmp_dir)
             if dl_path and Path(dl_path).is_file(): media_path = dl_path; media_acquired = True; print(f"[DEBUG media] Download successful: {media_path}")
             else: print(f"⚠️ Download failed/invalid path: {dl_path}"); media_path = None; media_acquired = False
         except Exception as e:
             print(f"❌ Download failed: {e}"); await ev.reply(f"⚠️ Download failed {media_source_info}. Text only.")
             media_path = None; media_acquired = False
             if tmp_dir: shutil.rmtree(tmp_dir, ignore_errors=True); print(f"[DEBUG media] Cleaned tmp dir {tmp_dir} after DL error."); tmp_dir = None

    # --- Combine Captions (No Forwarding) ---
    final_caption = command_caption or ""; original_text_str = original_text or ""
    if original_text_str and original_text_str.strip() != final_caption.strip():
        separator = "\n\n---\n" if final_caption else ""
        final_caption = f"{final_caption}{separator}{original_text_str}"; print("[DEBUG add] Appended original text.")
    if final_caption is None: final_caption = ""
    print(f"[DEBUG add] Final caption for schedule: '{final_caption[:100]}...'")

    # --- Schedule ---
    local_id = await schedule_reminder(ev, parsed_dt_utc, final_caption, media_path=media_path, tmp_dir_to_clean=tmp_dir)

    # --- Confirmation (Simplified) ---
    if local_id is not None:
        try:
            loc_dt_str = parsed_dt_utc.astimezone(TZ).strftime('%d-%m-%Y %H:%M %Z')
            await ev.reply(f"✅ Reminder scheduled! (ID: {local_id})\nTime: **{loc_dt_str}**", parse_mode="md")
        except Exception as e: print(f"Error formatting confirmation: {e}"); await ev.reply(f"✅ Reminder scheduled! (ID: {local_id})", parse_mode="md")


@client.on(events.NewMessage(pattern=CMD_LIST))
async def list_handler(ev):
    if not await is_allowed(ev): return
    print(f"[RECV /list] chat={ev.chat_id}, user={ev.sender_id}")
    now = datetime.now(timezone.utc)
    valid_reminders_local = []
    needs_resave = False

    # --- Cleanup Logic ---
    for r in list(reminders):
         if not (isinstance(r, dict) and all(k in r for k in ['id','chat_id','scheduled_id','time'])):
             print(f"⚠️ Removing invalid structure: {r}"); needs_resave = True
             try: reminders.remove(r)
             except ValueError: print(f"    Info: Invalid item {r.get('id', '(no id)')} not found.")
             continue
         try:
             r_time_utc = datetime.fromisoformat(r["time"])
             if r_time_utc.tzinfo is None:
                print(f"⚠️ ID {r.get('id')}: naive datetime, assuming UTC."); r_time_utc = r_time_utc.replace(tzinfo=timezone.utc); needs_resave = True
                updated = False; current_id = r.get('id')
                for i, item in enumerate(reminders):
                    if item.get("id") == current_id: reminders[i]["time"] = r_time_utc.isoformat(); updated = True; break
                if not updated: print(f"    Warning: Could not find ID {current_id} to update time.")
             if r_time_utc > now: valid_reminders_local.append(r)
             else:
                 print(f"ℹ️ Removing past id={r.get('id')}"); needs_resave = True
                 try: reminders.remove(r)
                 except ValueError: print(f"    Info: Past item {r.get('id', '(no id)')} not found.")
         except (ValueError, Exception) as e:
             print(f"⚠️ Removing invalid id={r.get('id')}: {e}"); needs_resave = True
             try: reminders.remove(r)
             except ValueError: print(f"    Info: Error item {r.get('id', '(no id)')} not found.")
             continue
    if needs_resave: print("ℹ️ Resaving reminders list after cleanup."); save_reminders()
    # --- End Cleanup ---

    active_reminders = sorted(valid_reminders_local, key=lambda r: datetime.fromisoformat(r["time"]))
    if not active_reminders: await ev.reply("ℹ️ No upcoming reminders."); return

    # --- User Info Fetch ---
    user_ids_needed = {r.get('user_id') for r in active_reminders if r.get('user_id')}
    user_info_cache = {}
    if user_ids_needed:
        print(f"[DEBUG list] Fetching info for user IDs: {user_ids_needed}")
        try:
            entities = await client.get_entity(list(user_ids_needed))
            if not isinstance(entities, list): entities = [entities]
            for user in entities:
                if user: user_info_cache[user.id] = user
        except Exception as fetch_err: print(f"⚠️ Error pre-fetching users: {fetch_err}")

    # --- Build Formatted Message ---
    message_parts = ["**🗓️ Upcoming Reminders:**\n"] # Start with bold title

    for r in active_reminders:
        try:
            # Reminder Header (Bold ID and Number)
            message_parts.append(f"**🗓️ ID {r.get('id', '?')}**") # <<< Bold ID, no italics

            # Date Line (Bold Label)
            when_local = datetime.fromisoformat(r["time"]).astimezone(TZ)
            time_str = when_local.strftime('%d-%m-%Y %H:%M %Z')
            message_parts.append(f"   • **Date:** {time_str}") # <<< Bold label

            # Reminder Text Line (Bold Label)
            caption = r.get('caption', '').strip()
            if not caption:
                reminder_text = "_(no description provided)_" # Keep italics for placeholder
            else:
                caption_clean = caption.replace('_', '').replace('*', '').replace('`','')
                max_caption_len = 70
                if len(caption_clean) > max_caption_len:
                    caption_clean = caption_clean[:max_caption_len-1] + "…"
                reminder_text = caption_clean
            message_parts.append(f"   • **Reminder:** {reminder_text}") # <<< Bold label

            # Creator Line (Bold Label)
            creator_name = f"ID {r.get('user_id', 'Unknown')}" # Default
            user_id = r.get('user_id')
            if user_id:
                user_entity = user_info_cache.get(user_id)
                if not user_entity and user_id not in user_info_cache:
                    try:
                        user_entity = await client.get_entity(user_id); user_info_cache[user_id] = user_entity
                    except Exception: user_info_cache[user_id] = None

                if user_entity:
                    username = getattr(user_entity, 'username', None)
                    first_name = getattr(user_entity, 'first_name', None)
                    if username:
                        creator_name = username # No '@'
                    elif first_name:
                        creator_name = first_name
            message_parts.append(f"   • **By:** {creator_name}") # <<< Bold label

            # Add a blank line for separation
            message_parts.append("")

        except Exception as e:
            print(f"Err format id={r.get('id','?')}: {e}");
            # Add error line in the new format
            message_parts.append(f"**🗓️ ID {r.get('id','?')}**") # Bold ID
            message_parts.append("   • **Error:** formatting this reminder.") # Bold label
            message_parts.append("")


    # --- Send the message ---
    final_message = "\n".join(message_parts).strip()

    if len(final_message) > 4096:
        final_message = final_message[:4050] + "\n\n... (list truncated)"

    await ev.reply(final_message, parse_mode="md")


@client.on(events.NewMessage(pattern=CMD_DEL))
async def delete_handler(ev):
    if not await is_allowed(ev): return; print(f"[RECV /delete] {ev.chat_id} {ev.sender_id} '{ev.raw_text}'")
    match = CMD_DEL.match(ev.raw_text)
    if not match: await ev.reply("❌ Use `/del reminder <ID>`."); return
    try: rid_to_delete = int(match.group(1))
    except ValueError: await ev.reply("❌ Invalid ID."); return
    reminder_to_delete = None; reminder_index = -1
    for i, r in enumerate(reminders):
        if isinstance(r, dict) and r.get("id") == rid_to_delete: reminder_to_delete = r; reminder_index = i; break
    if reminder_to_delete and reminder_index != -1:
        sched_id = reminder_to_delete.get("scheduled_id"); target_chat_id = reminder_to_delete.get("chat_id")
        deleted_from_tg = False; is_future = False
        try: is_future = datetime.fromisoformat(reminder_to_delete["time"]).replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)
        except: pass
        if sched_id and target_chat_id and is_future:
            try: print(f"[DEBUG delete] Attempt TG delete: sched={sched_id}"); await client.delete_scheduled_messages(target_chat_id, [sched_id]); print(f"✅ TG delete OK"); deleted_from_tg = True
            except Exception as e: print(f"⚠️ TG delete fail: {e}")
        elif not is_future: print(f"ℹ️ id={rid_to_delete} past, skip TG delete.")
        else: print(f"⚠️ id={rid_to_delete} lacks info, skip TG delete.")
        try:
             del reminders[reminder_index]; save_reminders(); print(f"🗑️ Removed id={rid_to_delete} locally.")
             tg_status = " (TG delete attempted)" if deleted_from_tg else ""
             await ev.reply(f"✅ Reminder **{rid_to_delete}** deleted{tg_status}.", parse_mode="md")
        except IndexError: print(f"⚠️ id={rid_to_delete} gone before local delete."); await ev.reply(f"ℹ️ ID **{rid_to_delete}** already removed?", parse_mode="md")
    else: print(f"[DEBUG delete] id={rid_to_delete} not found."); await ev.reply(f"❌ No active reminder **{rid_to_delete}**.", parse_mode="md")

@client.on(events.NewMessage(pattern=CMD_HELP))
async def help_handler(ev):
    if not await is_allowed(ev): return
    help_text = """**Reminder Bot Commands:**\n🗓️ `/add reminder <when> <your text>`\n   Schedule reminder. If replying, includes original text & media.\n   `<when>`: `tomorrow 9am`, `15-08-2024 10:00`, etc.\n📋 `/list reminders`\n   Show upcoming reminders in detailed format, including creator.\n🗑️ `/delete reminder <ID>`\n   Remove reminder by ID. Attempts TG delete."""
    await ev.reply(help_text, parse_mode="md")

# ─── RUN LOOP / MAIN FUNCTION ──────────────────────────────────────────────────
async def main():
    print("🚀 Reminder Userbot Starting..."); print(f"   Session: {SESSION_PATH}, TZ: {TZ_NAME}")
    try:
        print("🔗 Connecting..."); await client.start(); me = await client.get_me()
        print(f"✅ Connected as @{me.username}"); print(f"👂 Listening...")
        await client.run_until_disconnected()
    except (OperationalError, SessionPasswordNeededError, ConnectionError, asyncio.TimeoutError, TimeoutError, KeyboardInterrupt) as e:
        print(f"❌ STOPPED: {type(e).__name__}: {e}", file=sys.stderr)
        if isinstance(e, OperationalError): print("   Try deleting session file?", file=sys.stderr)
        if isinstance(e, SessionPasswordNeededError): print("   Run interactively once?", file=sys.stderr)
    except Exception as e: print(f"❌ UNEXPECTED CRITICAL ERROR: {e}"); import traceback; traceback.print_exc()
    finally:
        if client.is_connected(): print("\n🔌 Disconnecting..."); await client.disconnect()
        print("🛑 Reminder Userbot stopped.")

if __name__ == "__main__": asyncio.run(main())
