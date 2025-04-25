# Telegram Reminder Userbot [Working in 2025]

Easily schedule and send reminders—including text, images, videos, voice notes, and more—directly from your Telegram user account, using natural `/add reminder` commands and precise CEST/CET date handling.

---

## Key Capabilities

- **Flexible Scheduling**  
  Parse day‑first dates with optional times; defaults to 07:00 local if time is omitted.

- **Timezone Awareness**  
  Built on `zoneinfo` and `dateutil` for unambiguous CEST/CET interpretation and automatic UTC conversion.

- **Media Support**  
  Remind with attached media by replying to a message or including file uploads—images, videos, voice notes, documents, etc.

- **Allowed‑Chats Filter**  
  Restrict commands to one or more chat IDs or usernames for security and control.

- **Interactive Commands**  
  - `/add reminder <date> [time] <text>` – schedule a new reminder
  - `/list reminders` – list all upcoming scheduled reminders
  - `/delete reminder <ID>` – cancel a reminder by its Telegram-assigned message ID
  - `/help reminder` – show available commands

- **Debug Logging**  
  Auto‑logs parse input vs. now comparison for easy troubleshooting.

---

## How It Works

1. **Initialization**  
   On first run, the script loads or prompts for your `TG_API_ID`, `TG_API_HASH`, and optional `ALLOWED_CHATS` list, saving them to a `.env` file.

2. **Parsing & Validation**  
   Uses `dateutil.parser` with `dayfirst=True`, applies CEST/CET `zoneinfo`, defaults missing times to 07:00, and rejects past dates.

3. **Scheduling**  
   Calls Telethon’s `send_message` or `send_file` with the `schedule=` argument in UTC, so Telegram handles delivery at the precise moment.

4. **Media Handling**  
   Downloads reply‑or‑attached media into a temp folder, schedules sending the file, then cleans up.

5. **Systemd Integration**  
   Exposes an `async def main()` entrypoint for headless launch under systemd or other init systems.

6. **Commands & Help**  
   Listens for `/add reminder`, `/list reminders`, `/delete reminder`, and `/help reminder`—all case‑insensitive.

---

## Installation & Usage

1. **System Prep**
    ```bash
    sudo apt update && sudo apt upgrade -y
    sudo apt install git python3-venv python3-pip -y
    ```

2. **Clone & Setup**
    ```bash
    git clone https://github.com/Oliviertjeh/TelegramReminder
    cd TelegramReminder
    python3 -m venv .venv/TGreminder
    source .venv/TGreminder/bin/activate
    ```

3. **Configure**
    - Edit `.env` (or run once interactively) to set:
      ```dotenv
      TG_API_ID=123456
      TG_API_HASH=abcdef1234567890abcdef1234567890
      ALLOWED_CHATS=@mygroup,123456789  # optional
      TG_SESSION=reminder_session      # optional
      ```

4. **Run Manually**
    ```bash
    source .venv/TGreminder/bin/activate
    python TelegramReminder.py
    ```

5. **Run as a Service (systemd)**
    - Create `/etc/systemd/system/telegram_reminder.service` with content:
      ```ini
      [Unit]
      Description=Telegram Reminder Service
      After=network.target

      [Service]
      Type=simple
      User=pi
      WorkingDirectory=/home/pi/TGreminder
      EnvironmentFile=/home/pi/TGreminder/.env
      ExecStart=/home/pi/TGreminder/.venv/TGreminder/bin/python /home/pi/TGreminder/TelegramReminder_autorun.py
      Restart=on-failure

      [Install]
      WantedBy=multi-user.target
      ```
    - Enable and start:
      ```bash
      sudo systemctl daemon-reload
      sudo systemctl enable telegram_reminder
      sudo systemctl restart telegram_reminder
      sudo systemctl status telegram_reminder
      journalctl -u telegram_reminder -f
      ```

6. **Stopping Logs**  
   Press <kbd>Ctrl</kbd>+<kbd>C</kbd> in your SSH/terminal to exit live log view.

---

Now you’re all set—send a message like:
```
/add reminder 30-04-2025 18:00 Take out the trash
```
to schedule your first Telegram reminder!

