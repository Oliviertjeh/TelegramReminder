# Telegram Reminder Bot [Working 2025]

A versatile Telegram bot that allows you to schedule and manage reminders easily and directly within Telegram. You can send text-based reminders or include media like images and videos. The bot supports various date formats, timezones, and chat filtering for enhanced control and security.

---

## Features

- **Flexible Scheduling**
  - Ability to parse various date formats, including natural language input.
  - Timezone support with automatic conversion to UTC.
  - Defaults to 9:00 AM if no time is specified.

- **Media and Text Support**
  - Send reminders with attached media (images, videos, etc.).
  - Send text-based reminders.
  - Use text from replies as reminder captions.
  - Link to the original message when replying.
  - Media support can be toggled on/off based on directory permissions.

- **Chat Filtering**
  - Restrict commands to specific chat IDs or usernames for enhanced security.

- **Commands**
  - `/add <when> <msg>`: schedule a reminder.
  - `/list`: list upcoming reminders.
  - `/del <id>`: cancel a reminder by ID.
  - `/help`: show available commands.
  - `/start`: start bot message.

- **Robust Error Handling and Logging**
  - Detailed logging for debugging.
  - Handles missing dependencies gracefully.
  - Gracefully handle file errors.

- **Systemd Integration**
  - Designed for integration with systemd for automated background operation.

- **Storage**
  - Reminders are stored in a `reminders.json` file.

---

## How It Works

1. **Initialization**
   On first run, the script loads or prompts for your `TG_API_ID`, `TG_API_HASH`, and optional `ALLOWED_CHATS` list, saving them to a `.env` file.

2. **Parsing & Validation**  
   Uses `dateutil.parser` with `dayfirst=True`, applies CEST/CET `zoneinfo`, defaults missing times to 07:00, and rejects past dates.

3. **Scheduling**  
   Calls Telethon’s `send_message` or `send_file` with the `schedule=` argument in UTC, so Telegram handles delivery at the precise moment.
   
4. **Media Handling**
  Downloads reply‑or‑attached media into the `media` folder, schedules sending the file.
   
5. **Systemd Integration**
   Exposes an `async def main()` entrypoint for headless launch under systemd or other init systems.

6. **Commands & Help**  
   Listens for `/add reminder`, `/list reminders`, `/delete reminder`, and `/help reminder`—all case‑insensitive.

---

## Installation

1. **System Preparation**
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
    sudo mkdir /home/pi/TelegramReminder/sessions
    sudo chown pi:pi /home/pi/TelegramReminder/sessions
    sudo nano reminders.json
    sudo chmod u+rw reminders.json
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
    pip install python-dotenv
    pip install python-dateutil
    python3 -m pip install --upgrade telethon
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
      WorkingDirectory=/home/pi/TelegramReminder
      EnvironmentFile=/home/pi/TelegramReminder/.env
      ExecStart=/home/pi/TelegramReminder/.venv/TGreminder/bin/python /home/pi/TelegramReminder/TelegramReminder_autorun.py
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

