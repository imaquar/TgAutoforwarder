# TgAutoforwarder

A Python + Telethon service for automatically forwarding messages from multiple chats into one target chat.

## Features
- Listens for new messages in `SOURCE_CHATS`.
- Sends them to `TARGET_CHAT` either from your user account or from a bot.
- Adds a `[Source Chat Name]` prefix to the beginning of message text/caption.
- Preserves grouped media (albums) as grouped messages in the target chat.
- In `DELIVERY_MODE=user`, marks the target dialog as unread after each forwarded message.
- In `DELIVERY_MODE=bot`, edits the forwarded message when the source message is edited.
- Optional PM alerts: on a new private message from a user, bot sends `<Name> написал(-а) новое сообщение.` with per-sender cooldown.
- Supports login by phone code or by QR (`AUTH_MODE=qr`).

## Installation
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration
1. Get your `API_ID` and `API_HASH` from https://my.telegram.org.
2. Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```
3. Fill in the values in `.env`.

Example:
```env
API_ID=123456
API_HASH=xxxxxxxxxxxxxxxxxxxxxxxxxxxx
SESSION_NAME=autoforwarder
DELIVERY_MODE=user
AUTH_MODE=phone
SOURCE_CHATS=@chat_one,@chat_two
TARGET_CHAT=@my_target_chat
BOT_TOKEN=
BOT_TARGET_CHAT=
MESSAGE_MAP_FILE=autoforwarder_message_map.json
MESSAGE_MAP_TTL_DAYS=7
PM_ALERTS_ENABLED=false
PM_ALERT_TARGET_CHAT=
PM_ALERT_COOLDOWN_MINUTES=60
PM_ALERTS_FILE=autoforwarder_pm_alerts.json
SKIP_OUTGOING=true
ALLOWED_SENDERS=
CHAT_ALLOWED_SENDERS=
```

## Run
```bash
python forwarder.py
```

On first run in `AUTH_MODE=phone`, Telethon will ask for your phone number, login code, and 2FA password (if enabled).

For QR login, set `AUTH_MODE=qr`, run the script, and scan the terminal QR in Telegram: `Settings -> Devices -> Link Desktop Device`.

To deliver messages via bot, set:
```env
DELIVERY_MODE=bot
BOT_TOKEN=123456:your_bot_token
# optional, falls back to TARGET_CHAT
BOT_TARGET_CHAT=-1001234567890
```

To enable PM alerts:
```env
PM_ALERTS_ENABLED=true
# optional, defaults to BOT_TARGET_CHAT or TARGET_CHAT
PM_ALERT_TARGET_CHAT=-1001234567890
PM_ALERT_COOLDOWN_MINUTES=60
PM_ALERTS_FILE=autoforwarder_pm_alerts.json
```

To print available chats and IDs:
```bash
python forwarder.py --list-chats
```
Optional limit:
```bash
python forwarder.py --list-chats --list-limit 500
```

## Notes
- `SOURCE_CHATS` supports `@username`, links, and numeric IDs.
- `TARGET_CHAT` supports `@username`, links, and numeric IDs.
- `DELIVERY_MODE=bot` requires `BOT_TOKEN`.
- `BOT_TARGET_CHAT` is optional in bot mode; if empty, `TARGET_CHAT` is used.
- `MESSAGE_MAP_FILE` stores source->target message IDs for edit syncing in bot mode.
- `MESSAGE_MAP_TTL_DAYS` controls cleanup of old mapping records (`7` by default, `0` disables cleanup).
- Edit syncing works for messages that were forwarded while this mapping file was being maintained.
- `SKIP_OUTGOING=true` skips your own outgoing messages from `SOURCE_CHATS`; set it to `false` to forward your messages too.
- `ALLOWED_SENDERS` is optional and applies one sender list to all `SOURCE_CHATS`.
- `CHAT_ALLOWED_SENDERS` is optional JSON with per-chat sender lists and has priority over `ALLOWED_SENDERS`.
- Sender filters accept usernames and numeric IDs.
- For some media types where captions are not available, the service sends a separate prefix-only message as fallback.

### Optional PM Alerts
- `PM_ALERTS_ENABLED=true` enables this additional feature for incoming private messages only (not groups/channels).
- `PM_ALERT_TARGET_CHAT` is optional; if empty, alerts are sent to `BOT_TARGET_CHAT` (or `TARGET_CHAT`).
- `PM_ALERT_COOLDOWN_MINUTES` limits alerts to one per sender per cooldown window.
- `PM_ALERTS_FILE` persists PM alert cooldown state across restarts.

## Sender Filter Examples
Only selected senders from all source chats:
```env
ALLOWED_SENDERS=@boss,123456789
```

Different sender lists per chat:
```env
CHAT_ALLOWED_SENDERS={"@work_chat":["@boss","123456789"],"-1001234567890":["@teamlead","777000"]}
```
