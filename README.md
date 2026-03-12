# TgAutoforwarder

A Python + Telethon service for automatically forwarding messages from multiple chats into one target chat.

## Features
- Listens for new messages in `SOURCE_CHATS`.
- Sends them to `TARGET_CHAT`.
- Adds a `[Source Chat Name]` prefix to the beginning of message text/caption.
- Marks the target dialog as unread after each forwarded message.
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
AUTH_MODE=phone
SOURCE_CHATS=@chat_one,@chat_two
TARGET_CHAT=@my_target_chat
SKIP_OUTGOING=true
```

## Run
```bash
python forwarder.py
```

On first run in `AUTH_MODE=phone`, Telethon will ask for your phone number, login code, and 2FA password (if enabled).

For QR login, set `AUTH_MODE=qr`, run the script, and scan the terminal QR in Telegram: `Settings -> Devices -> Link Desktop Device`.

## Notes
- `SOURCE_CHATS` supports `@username`, links, and numeric IDs.
- `TARGET_CHAT` supports `@username`, links, and numeric IDs.
- For some media types where captions are not available, the service sends a separate prefix message and then forwards the original message.
