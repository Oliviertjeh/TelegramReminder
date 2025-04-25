#!/usr/bin/env python3
"""
reminder_userbot.py – Telegram reminder helper under a *user* account.

🔄 *2025-04-25*: Switched to `dateutil` + `zoneinfo` for reliable CEST/CET,
interactive setup, safe session handling, and now supports:
  • `/add reminder`
  • `/list reminders`
  • `/delete reminder <ID>`
with reminders stored in `reminders.json`. Fixed regex flags issue. Updated help text.
"""
import os
import re
import sys
import json
import tempfile
import shutil
import asyncio
from datetime import datetime, timezone, timedelta  # Added timedelta
from pathlib import Path

from dotenv import load_dotenv, set_key
from dateutil import parser as du_parser
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from sqlite3 import OperationalError
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError # Added ZoneInfoNotFoundError

# ─── CONFIG & ENV ──────────────────────────────────────────────────────────────
ROOT     = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
load_dotenv(ENV_PATH)

API_ID   = os.getenv("TG_API_ID")
API_HASH = os.getenv("TG_API_HASH")
CHAT_RAW = os.getenv("ALLOWED_CHATS", "").strip()
SESSION_NAME = os.getenv("TG_SESSION", "reminder_session")
# Allow timezone configuration via .env, default to Europe/Amsterdam
DEFAULT_TZ = "Europe/Amsterdam"
TZ_NAME  = os.getenv("TIMEZONE", DEFAULT_TZ)
try:
    TZ = ZoneInfo(TZ_NAME)
    print(f"ℹ️ Using timezone: {TZ_NAME}")
except ZoneInfoNotFoundError:
    print(f"⚠️ Warning: Timezone '{TZ_NAME}' not found. Falling back to '{DEFAULT_TZ}'.")
    TZ_NAME = DEFAULT_TZ
    TZ = ZoneInfo(TZ_NAME)
except Exception as e:
    print(f"⚠️ Unexpected error loading timezone '{TZ_NAME}': {e}. Falling back to '{DEFAULT_TZ}'.")
    TZ_NAME = DEFAULT_TZ
    TZ = ZoneInfo(TZ_NAME)


# Interactive prompts for missing ENV values -----------------------------------
def prompt_env(var, question, validate=lambda v: bool(v.strip())):
    # Create .env if it doesn't exist
    if not ENV_PATH.exists():
        try:
            ENV_PATH.touch()
            print(f"📝 Created .env file at: {ENV_PATH}")
        except OSError as e:
            print(f"❌ Critical: Could not create .env file at {ENV_PATH}: {e}")
            print("   Please check permissions for the script's directory.")
            sys.exit(1)

    while True:
        val = input(question).strip()
        if validate(val):
            try:
                # Use set_key which handles loading/saving correctly
                set_key(str(ENV_PATH), var, val, quote_mode='never')
                print(f"✅ Saved {var} to .env")
                return val
            except Exception as e:
                 print(f"❌ Error saving {var} to .env file: {e}")
                 # Don't exit, but inform user value might not be saved
                 return val # Return value even if saving failed, so script can proceed if possible
        print("❌ Invalid input; please try again.")

if not API_ID:
    API_ID = prompt_env("TG_API_ID",   "Enter your Telegram API ID: ",    lambda v: v.isdigit())
if not API_HASH:
    API_HASH = prompt_env("TG_API_HASH","Enter your Telegram API Hash: ", lambda v: len(v) >= 32)
# Only prompt for ALLOWED_CHATS if it's *not present* in the .env file at all
# This prevents re-prompting if the user deliberately set it to blank.
if os.getenv("ALLOWED_CHATS") is None:
    print("\nRestrict reminder commands to specific chats? (Optional)")
    print("Enter comma-separated chat IDs (like -100123...) or @usernames.")
    CHAT_RAW = input("Leave blank to allow commands in *all* your chats: ").strip()
    set_key(str(ENV_PATH), "ALLOWED_CHATS", CHAT_RAW, quote_mode='never')
    print(f"✅ Saved ALLOWED_CHATS setting to .env (blank means all allowed).")
else:
     CHAT_RAW = os.getenv("ALLOWED_CHATS", "").strip() # Ensure we use the loaded value


API_ID = int(API_ID)
ALLOWED_CHATS = {
    int(x) if x.lstrip("-+").isdigit() else x.lstrip("@").lower() # Ensure lowercase comparison for usernames
    for x in CHAT_RAW.split(',') if x.strip()
}

# ─── SESSION HANDLING ──────────────────────────────────────────────────────────
SESSION_DIR  = ROOT / "sessions"
SESSION_DIR.mkdir(exist_ok=True)
SESSION_PATH = SESSION_DIR / f"{SESSION_NAME}.session"
try:
    # Check directory write permissions first
    if not os.access(SESSION_DIR, os.W_OK):
         raise PermissionError(f"Cannot write to session directory: {SESSION_DIR}")
    exists   = SESSION_PATH.exists()
    writable = os.access(SESSION_PATH, os.W_OK) if exists else True # Check file write permission if it exists
except PermissionError as e:
    print(f"❌ Session directory/file permission error: {e}")
    print(f"   Ensure the directory '{SESSION_DIR}' and potentially the file '{SESSION_PATH}' are writable by the user running this script.")
    print(f"   Try running: sudo chown -R $(whoami):$(whoami) {SESSION_DIR.parent}") # chown parent dir might be needed if sessions/ doesn't exist initially
    sys.exit(1)

if exists and not writable:
    print(f"❌ Cannot write session file: {SESSION_PATH}")
    print(f"   Fix: sudo chown $(whoami):$(whoami) {SESSION_PATH}")
    sys.exit(1)

# ─── TELETHON CLIENT ───────────────────────────────────────────────────────────
# Consider adding retry logic or connection parameters if needed
client = TelegramClient(str(SESSION_PATH), API_ID, API_HASH, system_version="4.16.30-vxCUSTOM") # Example system_version

# ─── CONSTANTS ─────────────────────────────────────────────────────────────────
CMD_ADD  = re.compile(r"^/(?:add[_ ]?reminder)\s+(.+)", re.I | re.S)
CMD_LIST = re.compile(r"^/list(?:[_ ]?reminders)?$", re.I) # Allow /list or /list reminders
CMD_DEL  = re.compile(r"^/(?:delete|del)[_ ]?reminder\s+(\d+)$", re.I) # Allow /delete or /del
CMD_HELP = re.compile(r"^/help(?:[_ ]?reminder)?$", re.I) # Allow /help or /help reminder
TIME_RE  = re.compile(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\b") # More specific time regex

# ─── STORAGE (Robust Loading) ──────────────────────────────────────────────────
REMINDERS_PATH = ROOT / "reminders.json"
reminders = [] # Default to empty list

if REMINDERS_PATH.exists():
    print(f"💾 Loading reminders from {REMINDERS_PATH}...")
    try:
        loaded_data = json.loads(REMINDERS_PATH.read_text(encoding='utf-8'))
        if isinstance(loaded_data, list):
            valid_items = [r for r in loaded_data if isinstance(r, dict) and 'id' in r]
            if len(valid_items) != len(loaded_data):
                 print(f"⚠️ Warning: Filtered out {len(loaded_data) - len(valid_items)} non-dictionary or incomplete items from {REMINDERS_PATH}")
            reminders = valid_items
            print(f"✅ Loaded {len(reminders)} reminders.")
        else:
            print(f"⚠️ Warning: {REMINDERS_PATH} does not contain a JSON list. Initializing empty list.")
            # Consider backing up the invalid file
            # backup_path = REMINDERS_PATH.with_suffix(f".json.invalid.{datetime.now().strftime('%Y%m%d%H%M%S')}")
            # REMINDERS_PATH.rename(backup_path)
            # print(f"   Backed up invalid file to {backup_path}")
    except json.JSONDecodeError as e:
        print(f"⚠️ Warning: Invalid JSON in {REMINDERS_PATH}: {e}. Initializing empty list.")
        # Consider backing up
    except Exception as e: # Catch other potential errors like permission issues
        print(f"⚠️ Error reading {REMINDERS_PATH}: {e}. Initializing empty list.")
else:
    print(f"ℹ️ Reminders file not found ({REMINDERS_PATH}). Starting with no reminders.")

# Calculate next_id safely using only valid loaded dictionary items
next_id = max([r.get("id", 0) for r in reminders], default=0) + 1
print(f"ℹ️ Next reminder ID will be: {next_id}")

def save_reminders():
    # Ensure we only save valid dictionary reminders that likely came from schedule_reminder
    valid_reminders = [r for r in reminders if isinstance(r, dict) and "id" in r and "chat_id" in r and "scheduled_id" in r]
    print(f"💾 Saving {len(valid_reminders)} reminders to {REMINDERS_PATH}...")
    try:
        # Use atomic write pattern
        temp_path = REMINDERS_PATH.with_suffix('.tmp')
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(valid_reminders, f, indent=2, ensure_ascii=False)
        shutil.move(str(temp_path), str(REMINDERS_PATH)) # Atomic move/rename
        print(f"✅ Reminders saved successfully.")
    except Exception as e:
        print(f"❌ Error saving reminders to {REMINDERS_PATH}: {e}")
        # Attempt to remove temporary file if it exists
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass

# ─── PARSE DATETIME WITH DEBUG ─────────────────────────────────────────────────
def parse_dt(text: str):
    """Parses date/time string, assuming local timezone if unspecified."""
    now_local = datetime.now(TZ)
    try:
        # Settings for dateutil.parser
        parser_info = du_parser.parserinfo(dayfirst=True) # Assume European DD/MM/YY
        # Parse the text. Fuzzy might be too aggressive, start without it.
        dt_naive = du_parser.parse(text, parserinfo=parser_info, fuzzy=False)

        # Timezone handling:
        if dt_naive.tzinfo is None:
            # If parsed datetime is naive, assume it's in the script's configured local timezone (TZ)
            dt_local = TZ.localize(dt_naive)
        else:
            # If parsed datetime has timezone info, convert it to the script's local timezone (TZ)
            dt_local = dt_naive.astimezone(TZ)

        # Default time logic: If no time was explicitly parsed (still midnight)
        # AND the original text doesn't contain something looking like H:MM
        if dt_local.hour == 0 and dt_local.minute == 0 and dt_local.second == 0 and not TIME_RE.search(text):
            # Set a default time (e.g., 09:00 in the local timezone)
            default_time_hour = 9
            dt_local = dt_local.replace(hour=default_time_hour, minute=0, second=0, microsecond=0)
            print(f"[DEBUG parse_dt] No time found in '{text}', defaulting to {default_time_hour:02}:00 {TZ_NAME}")

        # Convert the final local datetime object to UTC for internal storage/scheduling
        dt_utc = dt_local.astimezone(timezone.utc)

        print(f"[DEBUG parse_dt] Input='{text}'")
        print(f"  Parsed Naive = {dt_naive}")
        print(f"  Interpreted Local ({TZ_NAME}) = {dt_local.strftime('%Y-%m-%d %H:%M:%S %Z%z')}")
        print(f"  Resulting UTC = {dt_utc.strftime('%Y-%m-%d %H:%M:%S %Z%z')}")
        return dt_utc

    except (du_parser.ParserError, ValueError) as e:
        print(f"[DEBUG parse_dt] Failed to parse '{text}': {e}")
        return None
    except Exception as e: # Catch unexpected errors
        print(f"[DEBUG parse_dt] Unexpected error parsing '{text}': {e}")
        return None


# ─── SCHEDULE REMINDER ─────────────────────────────────────────────────────────
async def schedule_reminder(ev, when: datetime, caption: str, media_path: str | None = None):
    """Schedules the message and stores reminder info."""
    global next_id
    if not isinstance(when, datetime) or when.tzinfo != timezone.utc:
        print(f"❌ Internal Error: schedule_reminder needs UTC datetime, got {when}")
        await ev.reply("❌ Internal error processing reminder time.")
        return None

    sender = await ev.get_sender()
    mention = f"[{sender.first_name or 'User'}](tg://user?id={sender.id})"
    text = f"⏰ {mention}: {caption}" if caption else f"⏰ Reminder for {mention}"

    # Ensure 'when' is slightly in the future for Telethon scheduling
    now = datetime.now(timezone.utc)
    min_schedule_delay = timedelta(seconds=10) # Minimum delay to avoid race conditions/API issues
    if when <= now + min_schedule_delay:
        when = now + min_schedule_delay
        print(f"⚠️ Scheduled time was too soon or in past, adjusting to {when.isoformat()}")

    try:
        print(f"[DEBUG schedule] Scheduling for chat={ev.chat_id}, time={when.isoformat()}, caption='{caption[:50]}...'")
        if media_path:
            msg = await client.send_file(ev.chat_id, media_path, caption=text, schedule=when, parse_mode="md")
        else:
            msg = await client.send_message(ev.chat_id, text, schedule=when, parse_mode="md")
        print(f"[DEBUG schedule] Successfully scheduled msg_id={msg.id}")

    except Exception as e:
        print(f"❌ Failed to schedule message in chat {ev.chat_id}: {e}")
        await ev.reply(f"❌ Failed to schedule message: {e}")
        return None

    # Store reminder details
    current_id = next_id
    reminder = {
        "id": current_id,
        "chat_id": ev.chat_id,
        "scheduled_id": msg.id,
        "time": when.isoformat(), # Store UTC time as ISO string
        "caption": caption,       # Store original caption
        "user_id": sender.id      # Store user ID for potential future use
    }
    reminders.append(reminder)
    save_reminders() # Save after successful scheduling and appending
    print(f"✅ Stored reminder locally: local_id={current_id}, chat={ev.chat_id}, sched_id={msg.id}, time={when.isoformat()}, caption='{caption[:50]}...'")
    next_id += 1
    return reminder["id"]

# ─── UTILITY: Check Chat Permission ────────────────────────────────────────────
async def is_allowed(ev):
    """Checks if the command is allowed in the current chat."""
    if not ALLOWED_CHATS:
        return True # Allow all if the list is empty

    chat_id = ev.chat_id
    if chat_id in ALLOWED_CHATS:
        return True

    try:
        chat = await ev.get_chat()
        uname = getattr(chat, "username", None)
        if uname and uname.lower() in ALLOWED_CHATS:
            return True
    except Exception as e:
        # Errors getting chat info shouldn't block if ID match failed anyway
        print(f"ℹ️ Could not get chat username for {chat_id} (error: {e}). Relying on ID check.")

    # If neither ID nor username matched
    print(f"🚫 Command denied: Chat ID {chat_id} / Username not in ALLOWED_CHATS.")
    # Optionally send a message back? Be careful not to spam.
    # await ev.reply("Sorry, I cannot perform this command in this chat.")
    return False

# ─── HANDLERS ─────────────────────────────────────────────────────────────────
@client.on(events.NewMessage(pattern=CMD_ADD))
async def add_handler(ev):
    """Handles the /add reminder command."""
    if not await is_allowed(ev): return
    print(f"[RECV /add] chat={ev.chat_id}, user={ev.sender_id}, msg='{ev.raw_text[:100]}...'")

    match = CMD_ADD.match(ev.raw_text)
    tail = match.group(1).strip() if match else ""
    if not tail:
        await ev.reply("⚠️ Usage: `/add reminder <date/time info> <reminder text>`\nExample: `/add reminder tomorrow 9am Check backup`", parse_mode="md")
        return

    # Attempt to parse date/time and separate caption text
    tokens = tail.split()
    parsed_dt_utc = None
    caption = ""

    # Iterate through potential date/time parts at the beginning
    for i in range(1, len(tokens) + 1):
        potential_date_str = " ".join(tokens[:i])
        temp_dt = parse_dt(potential_date_str)
        if temp_dt: # If parse_dt returns a valid datetime
             parsed_dt_utc = temp_dt
             caption = " ".join(tokens[i:]).strip()
             print(f"[DEBUG add] Parsed date='{potential_date_str}', caption='{caption}'")
             break # Stop on the first successful parse

    if not parsed_dt_utc:
        print(f"[DEBUG add] Could not parse date/time from start of: '{tail}'")
        await ev.reply("❌ Couldn't understand the date/time. Please try formats like `dd-mm-yyyy hh:mm`, `tomorrow 9am`, `next friday 17:00`, etc.", parse_mode="md")
        return

    now = datetime.now(timezone.utc)
    if parsed_dt_utc <= now:
        print(f"[DEBUG add] Parsed time is in the past: {parsed_dt_utc}")
        await ev.reply("⏳ The specified date/time is in the past!", parse_mode="md")
        return

    # Media handling
    media_path = None
    tmp_dir = None
    source_message = None
    if ev.media:
        source_message = ev
        print("[DEBUG add] Found media in the command message itself.")
    elif ev.is_reply:
        reply_msg = await ev.get_reply_message()
        if reply_msg and reply_msg.media:
            source_message = reply_msg
            print(f"[DEBUG add] Found media in the replied message (ID: {reply_msg.id}).")

    if source_message:
        try:
            tmp_dir = tempfile.mkdtemp(prefix="tgrem_media_")
            print(f"[DEBUG media] Downloading media from msg {source_message.id} to {tmp_dir}")
            # Limit download size? Add timeout?
            media_path = await source_message.download_media(file=tmp_dir)
            # Check if download actually returned a path
            if not media_path or not Path(media_path).is_file():
                 print(f"⚠️ Media download seemed to succeed but resulted path is invalid: {media_path}")
                 media_path = None # Ensure media_path is None if download failed
            else:
                 print(f"[DEBUG media] Downloaded successfully to: {media_path}")
        except Exception as e:
            print(f"❌ Failed to download media: {e}")
            await ev.reply("⚠️ Couldn't download the attached media. Scheduling text only.")
            media_path = None # Ensure media_path is None if download fails
            # Clean up temp dir even on failure
            if tmp_dir: shutil.rmtree(tmp_dir, ignore_errors=True)
            tmp_dir = None # Reset tmp_dir as we don't have a file in it


    # Schedule the reminder (pass media_path which might be None)
    local_id = await schedule_reminder(ev, parsed_dt_utc, caption, media_path)

    # Cleanup downloaded media directory if it exists and scheduling was attempted
    if tmp_dir:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        print(f"[DEBUG media] Cleaned up temporary directory: {tmp_dir}")

    # Send confirmation if scheduling was successful (local_id is not None)
    if local_id is not None:
        try:
            loc_dt_str = parsed_dt_utc.astimezone(TZ).strftime('%d-%m-%Y %H:%M %Z')
            await ev.reply(f"✅ Reminder scheduled! (ID: {local_id})\nTime: **{loc_dt_str}**", parse_mode="md")
        except Exception as e:
             print(f"Error formatting local time confirmation: {e}")
             await ev.reply(f"✅ Reminder scheduled! (ID: {local_id})", parse_mode="md") # Fallback reply

@client.on(events.NewMessage(pattern=CMD_LIST))
async def list_handler(ev):
    """Handles the /list reminders command."""
    if not await is_allowed(ev): return
    print(f"[RECV /list] chat={ev.chat_id}, user={ev.sender_id}")

    now = datetime.now(timezone.utc)
    active_reminders = []

    # Filter and sort valid reminders by time
    # Ensure 'time' exists and is a valid ISO string before comparing
    valid_reminders = []
    for r in reminders:
         if isinstance(r, dict) and "time" in r and "id" in r:
             try:
                 r_time = datetime.fromisoformat(r["time"])
                 if r_time > now: # Only include future reminders
                     valid_reminders.append(r)
             except ValueError:
                 print(f"⚠️ Found reminder ID {r.get('id')} with invalid time format: {r.get('time')}")
             except Exception as e:
                  print(f"⚠️ Error processing reminder ID {r.get('id')}: {e}")

    # Sort the valid future reminders
    active_reminders = sorted(valid_reminders, key=lambda r: datetime.fromisoformat(r["time"]))


    if not active_reminders:
        await ev.reply("ℹ️ You have no upcoming reminders scheduled.", parse_mode="md")
        return

    lines = ["**🗓️ Upcoming Reminders:**"]
    for r in active_reminders:
        try:
            when_utc = datetime.fromisoformat(r["time"])
            when_local = when_utc.astimezone(TZ)
            time_str = when_local.strftime('%d-%m-%y %H:%M %Z') # Shortened year
            caption_preview = r.get('caption', 'No text')
            # Truncate long captions for display
            if len(caption_preview) > 60:
                caption_preview = caption_preview[:57] + "..."
            lines.append(f" • ID **{r.get('id', '?')}**: `{time_str}` - _{caption_preview}_")
        except Exception as e:
            print(f"Error formatting reminder {r.get('id','?')}: {e}")
            lines.append(f" • ID {r.get('id','?')}: Error displaying reminder data.")

    # Consider Telegram message length limits if the list can be very long
    message_text = "\n".join(lines)
    if len(message_text) > 4000: # Approx limit
        message_text = "\n".join(lines[:50]) + "\n\n... (list too long, showing first 50)"

    await ev.reply(message_text, parse_mode="md")

@client.on(events.NewMessage(pattern=CMD_DEL))
async def delete_handler(ev):
    """Handles the /delete reminder command."""
    if not await is_allowed(ev): return
    print(f"[RECV /delete] chat={ev.chat_id}, user={ev.sender_id}, msg='{ev.raw_text}'")

    match = CMD_DEL.match(ev.raw_text)
    if not match: # Should not happen if pattern matches, but good practice
        await ev.reply("❌ Invalid command format. Use `/delete reminder <ID>`.", parse_mode="md")
        return

    try:
        rid_to_delete = int(match.group(1))
    except ValueError:
        await ev.reply("❌ Invalid reminder ID. It must be a number.", parse_mode="md")
        return

    reminder_to_delete = None
    reminder_index = -1
    for i, r in enumerate(reminders):
        # Check type and if 'id' key exists and matches
        if isinstance(r, dict) and r.get("id") == rid_to_delete:
            reminder_to_delete = r
            reminder_index = i
            break

    if reminder_to_delete and reminder_index != -1:
        sched_id = reminder_to_delete.get("scheduled_id")
        target_chat_id = reminder_to_delete.get("chat_id")

        if sched_id and target_chat_id:
            try:
                print(f"[DEBUG delete] Attempting Telegram delete: scheduled_id={sched_id}, chat_id={target_chat_id}")
                await client.delete_scheduled_messages(target_chat_id, [sched_id])
                print(f"✅ Successfully deleted scheduled message from Telegram (ID: {sched_id}).")
            except Exception as e:
                # Log error, but proceed to remove from local list anyway
                # Common errors: MESSAGE_ID_INVALID (already sent/deleted), CHAT_ADMIN_REQUIRED (if bot lost rights?)
                print(f"⚠️ Could not delete scheduled message ID {sched_id} from Telegram (might be already sent/deleted/no permission): {e}")
        else:
             print(f"⚠️ Reminder ID {rid_to_delete} lacks scheduled_id or chat_id, cannot delete from Telegram.")

        # Remove from local list using index
        del reminders[reminder_index]
        save_reminders() # Save the updated list
        print(f"🗑️ Removed reminder ID {rid_to_delete} from local store.")
        await ev.reply(f"✅ Reminder ID **{rid_to_delete}** deleted.", parse_mode="md")
    else:
        print(f"[DEBUG delete] Reminder ID {rid_to_delete} not found in local list.")
        await ev.reply(f"❌ No active reminder found with ID **{rid_to_delete}**.", parse_mode="md")
# ─── HELP ─────────────────────────────────────────────────────────────────────
@client.on(events.NewMessage(pattern=CMD_HELP))
async def help_handler(ev):
    """Send the available commands help text."""
    if not await is_allowed(ev): return
    help_text = """**Available commands:**

🗓️ `/add reminder date time message` – create a new reminder

📋 `/list reminders` – list active reminders

🗑️ `/delete reminder <ID>` – remove a reminder by its ID
"""
    await ev.reply(help_text, parse_mode="md")

# ─── RUN LOOP / MAIN FUNCTION ──────────────────────────────────────────────────
async def main():
    """Connects the client and runs until disconnected."""
    print("🚀 Reminder Userbot Initializing...")
    print(f"   Session: {SESSION_PATH}")
    print(f"   Timezone: {TZ_NAME}")
    print(f"   Reminders File: {REMINDERS_PATH}")
    print(f"   Allowed Chats: {'All' if not ALLOWED_CHATS else ', '.join(map(str, ALLOWED_CHATS))}")

    try:
        # Attempt to connect
        print("🔗 Connecting to Telegram...")
        # Use phone login if session invalid/missing, otherwise uses session file
        await client.start()

        # Log connection success
        me = await client.get_me()
        print(f"✅ Successfully connected as @{me.username} (ID: {me.id})")
        print(f"👂 Listening for commands...")

        # Keep the client running until disconnected or interrupted
        await client.run_until_disconnected()

    except OperationalError as e:
        print(f"❌ SQLite Database Error: {e}", file=sys.stderr)
        print("   This often means the session file is corrupted or locked by another process.", file=sys.stderr)
        print(f"   Suggestion: Stop the script, delete the session file ({SESSION_PATH}), and restart.", file=sys.stderr)
    except SessionPasswordNeededError:
        print("🔐 Two-Factor Authentication Required", file=sys.stderr)
        print("   Your account uses 2FA. The script needs your password to log in.", file=sys.stderr)
        print("   Please run the script *manually* in your terminal (not as a service) one time:", file=sys.stderr)
        print(f"      python3 {Path(__file__).name}", file=sys.stderr)
        print("   It will prompt you for the password. After successful login, it should work as a service.", file=sys.stderr)
    except (ConnectionError, asyncio.TimeoutError) as e:
        print(f"❌ Network Connection Error: {e}", file=sys.stderr)
        print("   Could not connect to Telegram. Check your internet connection and Telegram's status.", file=sys.stderr)
        print("   The script might retry automatically depending on systemd/supervisor setup.", file=sys.stderr)
    except Exception as e:
        print(f"❌ An Unexpected Critical Error Occurred: {e}", file=sys.stderr)
        # Detailed traceback for debugging unknown issues
        import traceback
        traceback.print_exc()
    finally:
        # Ensure client disconnects cleanly if it was connected
        if client.is_connected():
            print("\n🔌 Disconnecting from Telegram...")
            await client.disconnect()
        print("🛑 Reminder Userbot stopped.")

if __name__ == "__main__":
    try:
        # Use asyncio.run() for Python 3.7+ to handle the event loop
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🙏 User requested exit (Ctrl+C). Shutting down...")
    # The finally block in main() will handle disconnection.
