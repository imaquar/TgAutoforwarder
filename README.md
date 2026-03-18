# TgAutoforwarder

Telegram autoforwarder on Python + Telethon.

## Features
- Listens for new messages in `SOURCE_CHATS`.
- Sends them to `TARGET_CHAT` from your user account or from a bot.
- Adds a `[Source Chat Name]` prefix to message text/caption.
- For reply messages, includes quoted original message under the `[Source Chat Name]` prefix.
- Preserves grouped media (albums) as grouped messages in target chat.
- In `DELIVERY_MODE=user`, marks target dialog as unread after forwarding.
- Syncs edits: when source message is edited, forwarded text/caption is updated (`user` and `bot` modes).
- Optional PM alerts with cooldown, language (`eng` / `ru`), and scheduled auto-delete.
- Optional email delivery for source forwarding and PM alerts.
- Supports login by phone code or QR (`AUTH_MODE=qr`).

## 1. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Create `.env`

```bash
cp .env.example .env
```

## 3. Required: Telegram API

Get `API_ID` and `API_HASH` at `https://my.telegram.org`.

```env
API_ID=123456
API_HASH=your_api_hash_here
SESSION_NAME=autoforwarder
AUTH_MODE=phone
```

## 4. Forwarding route

```env
SOURCE_CHATS=@chat_one,@chat_two
TARGET_CHAT=@my_target_chat
FORWARDING_ENABLED=true
SKIP_OUTGOING=true
```

- `SOURCE_CHATS` and `TARGET_CHAT` support `@username`, links, and numeric IDs.
- `FORWARDING_ENABLED=false` disables forwarding from `SOURCE_CHATS` completely.
- If `FORWARDING_ENABLED=false`, enable `PM_ALERTS_ENABLED=true` to keep script active.
- `SKIP_OUTGOING=true` means your own outgoing messages from `SOURCE_CHATS` will be ignored.  
- Set `SKIP_OUTGOING=false` if you want to forward your own messages too.

## 5. Delivery mode

User mode:

```env
DELIVERY_MODE=user
```

Bot mode:

```env
DELIVERY_MODE=bot
BOT_TOKEN=123456:your_bot_token
# optional, if empty TARGET_CHAT is used
BOT_TARGET_CHAT=
```

`BOT_TOKEN` is required when `DELIVERY_MODE=bot`.

## 6. Edit sync map storage

```env
MESSAGE_MAP_FILE_USER=autoforwarder_message_map_user.json
MESSAGE_MAP_FILE_BOT=autoforwarder_message_map_bot.json
MESSAGE_MAP_TTL_DAYS=7
```

- `MESSAGE_MAP_FILE_USER`: where mapping is stored in `DELIVERY_MODE=user`.
- `MESSAGE_MAP_FILE_BOT`: where mapping is stored in `DELIVERY_MODE=bot`.
- `MESSAGE_MAP_TTL_DAYS`: auto-cleanup for old mapping records.
- Example: `7` means delete records older than 7 days, `0` disables cleanup.

## 7. Optional sender filters

Forward only specific senders from all source chats:

```env
ALLOWED_SENDERS=@boss,123456789
```

Per-chat filters (priority over `ALLOWED_SENDERS`):

```env
CHAT_ALLOWED_SENDERS={"@work_chat":["@boss","123456789"],"-1001234567890":["@teamlead","777000"]}
```

## 8. Optional PM alerts

```env
PM_ALERTS_ENABLED=true
# required for PM alerts in any mode
BOT_TOKEN=123456:your_bot_token
# optional, default is BOT_TARGET_CHAT or TARGET_CHAT
PM_ALERT_TARGET_CHAT=
PM_ALERT_COOLDOWN_MINUTES=60
PM_ALERTS_LANG=eng
PM_ALERTS_FILE=autoforwarder_pm_alerts.json
PM_ALERT_REQUIRE_MY_SILENCE=false
PM_ALERT_MIN_SILENCE_AFTER_MY_MESSAGE_MINUTES=30
PM_ALERT_MY_ACTIVITY_FILE=autoforwarder_pm_alerts_my_activity.json
PM_ALERTS_AUTO_DELETE_ENABLED=false
PM_ALERTS_AUTO_DELETE_TIME=05:00
PM_ALERTS_AUTO_DELETE_AFTER_HOURS=24
PM_ALERTS_AUTO_DELETE_FILE=autoforwarder_pm_alerts_messages.json
# optional ignore list
PM_ALERTS_EXCLUDE_CHATS=@john,123456789
```

- `PM_ALERTS_ENABLED`: turns private-message alerts on/off.
- `PM_ALERT_TARGET_CHAT`: where alerts are sent. If empty, fallback is `BOT_TARGET_CHAT` then `TARGET_CHAT`.
- `PM_ALERT_COOLDOWN_MINUTES`: minimum interval between alerts from the same sender.
- Example: `60` means one alert per sender per 60 minutes, `0` disables cooldown.
- `PM_ALERTS_LANG`: alert text language (`eng` or `ru`).
- `PM_ALERTS_FILE`: file with cooldown state, so limits survive restarts.
- `PM_ALERT_REQUIRE_MY_SILENCE`: extra guard for active dialogs.
- `PM_ALERT_MIN_SILENCE_AFTER_MY_MESSAGE_MINUTES`: PM alert is sent only if you did not message that chat for at least N minutes.
- `PM_ALERT_MY_ACTIVITY_FILE`: file with timestamps of your own PM activity.
- `PM_ALERTS_AUTO_DELETE_ENABLED`: enable scheduled deletion of PM alert messages.
- `PM_ALERTS_AUTO_DELETE_TIME`: daily delete time (`HH:MM`, server local time), for example `05:00`.
- `PM_ALERTS_AUTO_DELETE_AFTER_HOURS`: delete alerts older than this number of hours.
- Maximum for `PM_ALERTS_AUTO_DELETE_AFTER_HOURS` is `48` hours.
- `PM_ALERTS_AUTO_DELETE_FILE`: file with PM alert message IDs used by auto-delete.
- `PM_ALERTS_EXCLUDE_CHATS`: users/chats to ignore for PM alerts.

PM alerts text:

- `eng`: `<Name> sent a new message`
- `ru`: `<Name> отправил(-а) новое сообщение`

If you want only PM alerts and no chat forwarding:
```env
FORWARDING_ENABLED=false
PM_ALERTS_ENABLED=true
```

## 9. Optional email delivery

```env
# email copy of SOURCE_CHATS forwarding
EMAIL_FORWARDING_ENABLED=false

# email copy of PM alerts
EMAIL_PM_ALERTS_ENABLED=false

EMAIL_SMTP_HOST=
EMAIL_SMTP_PORT=587
EMAIL_USE_TLS=true
EMAIL_SMTP_USERNAME=
EMAIL_SMTP_PASSWORD=
EMAIL_FROM=
EMAIL_TO=me@example.com,backup@example.com
```

- `EMAIL_FORWARDING_ENABLED`: send forwarded source messages to email.
- `EMAIL_PM_ALERTS_ENABLED`: send PM alerts to email.
- `EMAIL_PM_ALERTS_ENABLED` requires `PM_ALERTS_ENABLED=true`.
- If at least one email flag is `true`, SMTP settings and `EMAIL_TO` are required.
- Email subject for forwarding is the source chat title.
- Email subject for PM alerts is the sender name.

## 10. Run

```bash
python forwarder.py
```

If `AUTH_MODE=phone`, Telethon asks for phone/code/2FA.

If `AUTH_MODE=qr`, scan terminal QR in Telegram:
`Settings -> Devices -> Link Desktop Device`.

## 11. Useful commands

List chats and IDs:

```bash
python forwarder.py --list-chats
```

With custom limit:

```bash
python forwarder.py --list-chats --list-limit 500
```

## 12. Notes

- In `DELIVERY_MODE=user`, target chat is marked unread after forwarding.
- Edit sync works for messages that were forwarded while map file was maintained.
- `MESSAGE_MAP_TTL_DAYS=0` disables cleanup.
- Some media types may not support caption edits on Telegram side.
