import asyncio
import argparse
from contextlib import suppress
from datetime import datetime, timedelta
from email.message import EmailMessage
from getpass import getpass
import html
import json
import logging
import mimetypes
import os
import smtplib
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Iterable

from dotenv import load_dotenv
import qrcode
from telethon import TelegramClient, errors, events, functions, types
from telethon.utils import get_peer_id


@dataclass
class Settings:
    api_id: int
    api_hash: str
    session_name: str
    forwarding_enabled: bool
    source_chats: list[str]
    target_chat: str | int | None
    delivery_mode: str
    bot_token: str | None
    bot_target_chat: str | int | None
    auth_mode: str
    skip_outgoing: bool
    allowed_senders: list[str]
    chat_allowed_senders: dict[str, list[str]]
    message_map_file_bot: str
    message_map_file_user: str
    message_map_ttl_days: int
    pm_alerts_enabled: bool
    pm_alert_target_chat: str | int | None
    pm_alert_cooldown_minutes: int
    pm_alerts_lang: str
    pm_alerts_file: str
    pm_alerts_exclude_chats: list[str]
    pm_alert_require_my_silence: bool
    pm_alert_min_silence_after_my_message_minutes: int
    pm_alert_my_activity_file: str
    pm_alerts_auto_delete_enabled: bool
    pm_alerts_auto_delete_hour: int
    pm_alerts_auto_delete_minute: int
    pm_alerts_auto_delete_after_hours: int
    pm_alerts_auto_delete_file: str
    email_forwarding_enabled: bool
    email_pm_alerts_enabled: bool
    email_smtp_host: str | None
    email_smtp_port: int
    email_use_tls: bool
    email_smtp_username: str | None
    email_smtp_password: str | None
    email_from: str | None
    email_to: list[str]


class MessageMapStore:
    def __init__(self, path: str, ttl_days: int = 7) -> None:
        self.path = path
        self.ttl_seconds: int | None = ttl_days * 24 * 60 * 60 if ttl_days > 0 else None
        self._lock = asyncio.Lock()
        self._data: dict[str, dict[str, int]] = {}
        self._load()

    @staticmethod
    def _key(source_chat_id: int, source_message_id: int) -> str:
        return f"{source_chat_id}:{source_message_id}"

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return

        try:
            with open(self.path, "r", encoding="utf-8") as file_obj:
                payload = json.load(file_obj)
            if isinstance(payload, dict):
                now = int(time.time())
                normalized: dict[str, dict[str, int]] = {}
                for key, value in payload.items():
                    if isinstance(value, int):
                        normalized[str(key)] = {
                            "target_message_id": int(value),
                            "updated_at": now,
                        }
                        continue

                    if isinstance(value, dict):
                        target_id = value.get("target_message_id")
                        updated_at = value.get("updated_at", now)
                        if isinstance(target_id, int):
                            normalized[str(key)] = {
                                "target_message_id": int(target_id),
                                "updated_at": int(updated_at) if isinstance(updated_at, int) else now,
                            }

                self._data = normalized
                self._prune_old_records_locked()
                self._save()
        except Exception as exc:
            logging.warning("Failed to load message map file %s: %s", self.path, exc)
            self._data = {}

    def _save(self) -> None:
        temp_path = f"{self.path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as file_obj:
            json.dump(self._data, file_obj)
        os.replace(temp_path, self.path)

    def _prune_old_records_locked(self) -> int:
        if self.ttl_seconds is None:
            return 0

        cutoff = int(time.time()) - self.ttl_seconds
        keys_to_remove = [
            key
            for key, value in self._data.items()
            if int(value.get("updated_at", 0)) < cutoff
        ]
        for key in keys_to_remove:
            self._data.pop(key, None)
        return len(keys_to_remove)

    async def get(self, source_chat_id: int, source_message_id: int) -> int | None:
        async with self._lock:
            value = self._data.get(self._key(source_chat_id, source_message_id))
            if not value:
                return None
            return int(value["target_message_id"])

    async def set(self, source_chat_id: int, source_message_id: int, target_message_id: int) -> None:
        async with self._lock:
            self._data[self._key(source_chat_id, source_message_id)] = {
                "target_message_id": target_message_id,
                "updated_at": int(time.time()),
            }
            self._prune_old_records_locked()
            self._save()


class PmAlertCooldownStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._data: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return

        try:
            with open(self.path, "r", encoding="utf-8") as file_obj:
                payload = json.load(file_obj)
            if isinstance(payload, dict):
                self._data = {
                    str(key): int(value)
                    for key, value in payload.items()
                    if isinstance(value, int)
                }
        except Exception as exc:
            logging.warning("Failed to load PM alerts file %s: %s", self.path, exc)
            self._data = {}

    def _save(self) -> None:
        temp_path = f"{self.path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as file_obj:
            json.dump(self._data, file_obj)
        os.replace(temp_path, self.path)

    def _prune_old_records_locked(self, cooldown_seconds: int) -> None:
        min_ttl = 7 * 24 * 60 * 60
        keep_for = max(cooldown_seconds * 2, min_ttl)
        cutoff = int(time.time()) - keep_for

        keys_to_remove = [key for key, ts in self._data.items() if ts < cutoff]
        for key in keys_to_remove:
            self._data.pop(key, None)

    async def should_notify(self, sender_id: int, cooldown_seconds: int) -> bool:
        async with self._lock:
            now = int(time.time())
            key = str(sender_id)
            last_alert = self._data.get(key)

            if cooldown_seconds > 0 and last_alert is not None and (now - last_alert) < cooldown_seconds:
                return False

            self._data[key] = now
            self._prune_old_records_locked(cooldown_seconds)
            self._save()
            return True


class PmAlertMyActivityStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._data: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return

        try:
            with open(self.path, "r", encoding="utf-8") as file_obj:
                payload = json.load(file_obj)
            if isinstance(payload, dict):
                self._data = {
                    str(key): int(value)
                    for key, value in payload.items()
                    if isinstance(value, int)
                }
        except Exception as exc:
            logging.warning("Failed to load PM alerts my-activity file %s: %s", self.path, exc)
            self._data = {}

    def _save(self) -> None:
        temp_path = f"{self.path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as file_obj:
            json.dump(self._data, file_obj)
        os.replace(temp_path, self.path)

    def _prune_old_records_locked(self) -> None:
        keep_for = 7 * 24 * 60 * 60
        cutoff = int(time.time()) - keep_for
        keys_to_remove = [key for key, ts in self._data.items() if ts < cutoff]
        for key in keys_to_remove:
            self._data.pop(key, None)

    async def touch_my_message(self, peer_id: int) -> None:
        async with self._lock:
            self._data[str(peer_id)] = int(time.time())
            self._prune_old_records_locked()
            self._save()

    async def has_required_silence(self, peer_id: int, min_silence_seconds: int) -> bool:
        async with self._lock:
            last_ts = self._data.get(str(peer_id))
            if last_ts is None:
                return True
            now = int(time.time())
            return (now - last_ts) >= min_silence_seconds


class PmAlertMessagesStore:
    def __init__(self, path: str, keep_days: int = 7) -> None:
        self.path = path
        self.keep_seconds = keep_days * 24 * 60 * 60
        self._lock = asyncio.Lock()
        self._data: dict[str, dict[str, int]] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return

        try:
            with open(self.path, "r", encoding="utf-8") as file_obj:
                payload = json.load(file_obj)
            if isinstance(payload, dict):
                normalized: dict[str, dict[str, int]] = {}
                for chat_id, bucket in payload.items():
                    if not isinstance(bucket, dict):
                        continue
                    normalized_bucket: dict[str, int] = {
                        str(message_id): int(sent_ts)
                        for message_id, sent_ts in bucket.items()
                        if isinstance(sent_ts, int)
                    }
                    if normalized_bucket:
                        normalized[str(chat_id)] = normalized_bucket
                self._data = normalized
                self._prune_old_records_locked()
                self._save()
        except Exception as exc:
            logging.warning("Failed to load PM alerts messages file %s: %s", self.path, exc)
            self._data = {}

    def _save(self) -> None:
        temp_path = f"{self.path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as file_obj:
            json.dump(self._data, file_obj)
        os.replace(temp_path, self.path)

    def _prune_old_records_locked(self) -> None:
        cutoff = int(time.time()) - self.keep_seconds
        empty_chat_ids: list[str] = []
        for chat_id, bucket in self._data.items():
            expired_message_ids = [message_id for message_id, sent_ts in bucket.items() if sent_ts < cutoff]
            for message_id in expired_message_ids:
                bucket.pop(message_id, None)
            if not bucket:
                empty_chat_ids.append(chat_id)
        for chat_id in empty_chat_ids:
            self._data.pop(chat_id, None)

    async def add(self, chat_id: int, message_id: int) -> None:
        async with self._lock:
            chat_key = str(chat_id)
            bucket = self._data.setdefault(chat_key, {})
            bucket[str(message_id)] = int(time.time())
            self._prune_old_records_locked()
            self._save()

    async def get_expired_ids(self, chat_id: int, cutoff_ts: int, limit: int = 5000) -> list[int]:
        async with self._lock:
            bucket = self._data.get(str(chat_id), {})
            expired = [
                int(message_id)
                for message_id, sent_ts in bucket.items()
                if sent_ts <= cutoff_ts
            ]
            expired.sort()
            return expired[:limit]

    async def remove_many(self, chat_id: int, message_ids: list[int]) -> None:
        if not message_ids:
            return
        async with self._lock:
            chat_key = str(chat_id)
            bucket = self._data.get(chat_key)
            if not bucket:
                return
            for message_id in message_ids:
                bucket.pop(str(message_id), None)
            if not bucket:
                self._data.pop(chat_key, None)
            self._prune_old_records_locked()
            self._save()


class EmailSender:
    def __init__(
        self,
        *,
        smtp_host: str,
        smtp_port: int,
        use_tls: bool,
        smtp_username: str | None,
        smtp_password: str | None,
        from_addr: str,
        to_addrs: list[str],
    ) -> None:
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.use_tls = use_tls
        self.smtp_username = smtp_username
        self.smtp_password = smtp_password
        self.from_addr = from_addr
        self.to_addrs = to_addrs

    def _send_sync(
        self,
        *,
        subject: str,
        body: str,
        attachments: list[tuple[str, str]],
    ) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.from_addr
        message["To"] = ", ".join(self.to_addrs)
        message.set_content(body)

        for file_path, attachment_name in attachments:
            mime_type, _ = mimetypes.guess_type(attachment_name)
            if mime_type:
                maintype, subtype = mime_type.split("/", 1)
            else:
                maintype, subtype = "application", "octet-stream"
            with open(file_path, "rb") as file_obj:
                file_data = file_obj.read()
            message.add_attachment(
                file_data,
                maintype=maintype,
                subtype=subtype,
                filename=attachment_name,
            )

        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as smtp:
            smtp.ehlo()
            if self.use_tls:
                smtp.starttls()
                smtp.ehlo()
            if self.smtp_username:
                smtp.login(self.smtp_username, self.smtp_password or "")
            smtp.send_message(message)

    async def send(
        self,
        *,
        subject: str,
        body: str,
        attachments: list[tuple[str, str]] | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._send_sync,
            subject=subject,
            body=body,
            attachments=attachments or [],
        )


def _parse_bool(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_auth_mode(value: str | None) -> str:
    mode = (value or "phone").strip().lower()
    if mode not in {"phone", "qr"}:
        raise ValueError("AUTH_MODE must be either 'phone' or 'qr'")
    return mode


def _parse_delivery_mode(value: str | None) -> str:
    mode = (value or "user").strip().lower()
    if mode not in {"user", "bot"}:
        raise ValueError("DELIVERY_MODE must be either 'user' or 'bot'")
    return mode


def _parse_pm_alerts_lang(value: str | None) -> str:
    raw = (value or "eng").strip().lower()
    aliases = {
        "ru": "ru",
        "rus": "ru",
        "eng": "eng",
        "en": "eng",
    }
    if raw not in aliases:
        raise ValueError("PM_ALERTS_LANG must be either 'ru' or 'eng'")
    return aliases[raw]


def _parse_refs_csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _parse_emails_csv(value: str | None) -> list[str]:
    candidates = [item.strip() for item in (value or "").split(",") if item.strip()]
    result: list[str] = []
    for candidate in candidates:
        if "@" not in candidate:
            raise ValueError("EMAIL_TO must contain valid email addresses")
        result.append(candidate)
    return result


def _parse_non_negative_int(value: str | None, default: int, var_name: str) -> int:
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value.strip())
    except ValueError as exc:
        raise ValueError(f"{var_name} must be a non-negative integer") from exc
    if parsed < 0:
        raise ValueError(f"{var_name} must be a non-negative integer")
    return parsed


def _parse_time_of_day(value: str | None, var_name: str, default: str = "05:00") -> tuple[int, int]:
    raw = (value or default).strip()
    parts = raw.split(":")
    if len(parts) != 2:
        raise ValueError(f"{var_name} must be in HH:MM format")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise ValueError(f"{var_name} must be in HH:MM format") from exc
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"{var_name} must be in HH:MM format")
    return hour, minute


def _parse_chat_allowed_senders(raw: str | None) -> dict[str, list[str]]:
    if not raw or not raw.strip():
        return {}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("CHAT_ALLOWED_SENDERS must be valid JSON") from exc

    if not isinstance(payload, dict):
        raise ValueError("CHAT_ALLOWED_SENDERS must be a JSON object")

    result: dict[str, list[str]] = {}
    for chat_ref, sender_refs in payload.items():
        if not isinstance(chat_ref, str):
            raise ValueError("CHAT_ALLOWED_SENDERS keys must be strings")
        if not isinstance(sender_refs, list):
            raise ValueError("CHAT_ALLOWED_SENDERS values must be arrays")

        normalized_refs: list[str] = []
        for sender_ref in sender_refs:
            normalized = str(sender_ref).strip()
            if normalized:
                normalized_refs.append(normalized)

        if not normalized_refs:
            raise ValueError(f"CHAT_ALLOWED_SENDERS entry for '{chat_ref}' cannot be empty")
        result[chat_ref.strip()] = normalized_refs

    return result


def load_settings(require_routing: bool = True) -> Settings:
    load_dotenv()

    api_id_raw = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    source_chats_raw = os.getenv("SOURCE_CHATS", "")
    target_chat = os.getenv("TARGET_CHAT", "")
    forwarding_enabled = _parse_bool(os.getenv("FORWARDING_ENABLED"), default=True)
    delivery_mode = _parse_delivery_mode(os.getenv("DELIVERY_MODE"))
    bot_token = (os.getenv("BOT_TOKEN") or "").strip() or None
    bot_target_chat_raw = (os.getenv("BOT_TARGET_CHAT") or "").strip()
    pm_alerts_enabled = _parse_bool(os.getenv("PM_ALERTS_ENABLED"), default=False)
    pm_alert_target_chat_raw = (os.getenv("PM_ALERT_TARGET_CHAT") or "").strip()
    pm_alert_require_my_silence = _parse_bool(os.getenv("PM_ALERT_REQUIRE_MY_SILENCE"), default=False)
    pm_alerts_auto_delete_enabled = _parse_bool(os.getenv("PM_ALERTS_AUTO_DELETE_ENABLED"), default=False)
    email_forwarding_enabled = _parse_bool(os.getenv("EMAIL_FORWARDING_ENABLED"), default=False)
    email_pm_alerts_enabled = _parse_bool(os.getenv("EMAIL_PM_ALERTS_ENABLED"), default=False)
    email_smtp_host = (os.getenv("EMAIL_SMTP_HOST") or "").strip() or None
    email_smtp_username = (os.getenv("EMAIL_SMTP_USERNAME") or "").strip() or None
    email_smtp_password = (os.getenv("EMAIL_SMTP_PASSWORD") or "").strip() or None
    email_from = (os.getenv("EMAIL_FROM") or "").strip() or None
    email_to = _parse_emails_csv(os.getenv("EMAIL_TO"))
    email_use_tls = _parse_bool(os.getenv("EMAIL_USE_TLS"), default=True)

    if not api_id_raw:
        raise ValueError("Environment variable API_ID is required")
    if not api_hash:
        raise ValueError("Environment variable API_HASH is required")

    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise ValueError("API_ID must be an integer") from exc

    session_name = os.getenv("SESSION_NAME", "autoforwarder")
    default_message_map_file_bot = f"{session_name}_message_map_bot.json"
    default_message_map_file_user = f"{session_name}_message_map_user.json"
    pm_alerts_auto_delete_hour, pm_alerts_auto_delete_minute = _parse_time_of_day(
        os.getenv("PM_ALERTS_AUTO_DELETE_TIME"),
        var_name="PM_ALERTS_AUTO_DELETE_TIME",
        default="05:00",
    )
    pm_alerts_auto_delete_after_hours = _parse_non_negative_int(
        os.getenv("PM_ALERTS_AUTO_DELETE_AFTER_HOURS"),
        default=24,
        var_name="PM_ALERTS_AUTO_DELETE_AFTER_HOURS",
    )
    pm_alert_min_silence_after_my_message_minutes = _parse_non_negative_int(
        os.getenv("PM_ALERT_MIN_SILENCE_AFTER_MY_MESSAGE_MINUTES"),
        default=30,
        var_name="PM_ALERT_MIN_SILENCE_AFTER_MY_MESSAGE_MINUTES",
    )
    email_smtp_port = _parse_non_negative_int(
        os.getenv("EMAIL_SMTP_PORT"),
        default=587,
        var_name="EMAIL_SMTP_PORT",
    )

    source_chats: list[str] = [item.strip() for item in source_chats_raw.split(",") if item.strip()]
    target_chat_ref: str | int | None = _coerce_ref(target_chat.strip()) if target_chat.strip() else None
    bot_target_chat_ref: str | int | None = _coerce_ref(bot_target_chat_raw) if bot_target_chat_raw else None
    pm_alert_target_chat_ref: str | int | None = None
    source_delivery_enabled = forwarding_enabled or email_forwarding_enabled
    pm_alerts_active = pm_alerts_enabled or email_pm_alerts_enabled
    if require_routing and source_delivery_enabled:
        if not source_chats:
            raise ValueError("Environment variable SOURCE_CHATS is required")
    if require_routing and forwarding_enabled:
        if target_chat_ref is None:
            raise ValueError("Environment variable TARGET_CHAT is required")

    if require_routing and not source_delivery_enabled and not pm_alerts_active:
        raise ValueError("Nothing to run: enable forwarding, PM alerts, or email delivery")

    if pm_alerts_auto_delete_enabled and not pm_alerts_enabled:
        raise ValueError("PM_ALERTS_AUTO_DELETE_ENABLED=true requires PM_ALERTS_ENABLED=true")
    if pm_alert_require_my_silence and not pm_alerts_active:
        raise ValueError("PM_ALERT_REQUIRE_MY_SILENCE=true requires PM alerts delivery enabled")

    if pm_alerts_auto_delete_after_hours > 48:
        raise ValueError("PM_ALERTS_AUTO_DELETE_AFTER_HOURS cannot be greater than 48")
    if pm_alerts_auto_delete_enabled and pm_alerts_auto_delete_after_hours < 1:
        raise ValueError("PM_ALERTS_AUTO_DELETE_AFTER_HOURS must be at least 1 when PM alerts auto-delete is enabled")

    if (forwarding_enabled and delivery_mode == "bot") or pm_alerts_enabled:
        if not bot_token:
            raise ValueError(
                "Environment variable BOT_TOKEN is required when DELIVERY_MODE=bot and forwarding is enabled, "
                "or when PM alerts Telegram delivery is enabled (PM_ALERTS_ENABLED=true)"
            )

    if email_smtp_port < 1:
        raise ValueError("EMAIL_SMTP_PORT must be a positive integer")

    email_any_enabled = email_forwarding_enabled or email_pm_alerts_enabled
    if email_any_enabled:
        if not email_smtp_host:
            raise ValueError("EMAIL_SMTP_HOST is required when email delivery is enabled")
        if not email_from:
            raise ValueError("EMAIL_FROM is required when email delivery is enabled")
        if not email_to:
            raise ValueError("EMAIL_TO is required when email delivery is enabled")
        if email_smtp_username and not email_smtp_password:
            raise ValueError("EMAIL_SMTP_PASSWORD is required when EMAIL_SMTP_USERNAME is set")

    if forwarding_enabled and delivery_mode == "bot":
        bot_target_chat_ref = bot_target_chat_ref or target_chat_ref

    if pm_alerts_enabled:
        default_pm_alert_target = bot_target_chat_ref or target_chat_ref
        pm_alert_target_chat_ref = _coerce_ref(pm_alert_target_chat_raw) if pm_alert_target_chat_raw else default_pm_alert_target
        if pm_alert_target_chat_ref is None:
            raise ValueError("Could not resolve PM alerts target chat. Set PM_ALERT_TARGET_CHAT explicitly.")

    return Settings(
        api_id=api_id,
        api_hash=api_hash,
        session_name=session_name,
        forwarding_enabled=forwarding_enabled,
        source_chats=source_chats,
        target_chat=target_chat_ref,
        delivery_mode=delivery_mode,
        bot_token=bot_token,
        bot_target_chat=bot_target_chat_ref,
        auth_mode=_parse_auth_mode(os.getenv("AUTH_MODE")),
        skip_outgoing=_parse_bool(os.getenv("SKIP_OUTGOING"), default=True),
        allowed_senders=_parse_refs_csv(os.getenv("ALLOWED_SENDERS")),
        chat_allowed_senders=_parse_chat_allowed_senders(os.getenv("CHAT_ALLOWED_SENDERS")),
        message_map_file_bot=(os.getenv("MESSAGE_MAP_FILE_BOT") or "").strip() or default_message_map_file_bot,
        message_map_file_user=(os.getenv("MESSAGE_MAP_FILE_USER") or "").strip() or default_message_map_file_user,
        message_map_ttl_days=_parse_non_negative_int(
            os.getenv("MESSAGE_MAP_TTL_DAYS"),
            default=7,
            var_name="MESSAGE_MAP_TTL_DAYS",
        ),
        pm_alerts_enabled=pm_alerts_enabled,
        pm_alert_target_chat=pm_alert_target_chat_ref,
        pm_alert_cooldown_minutes=_parse_non_negative_int(
            os.getenv("PM_ALERT_COOLDOWN_MINUTES"),
            default=60,
            var_name="PM_ALERT_COOLDOWN_MINUTES",
        ),
        pm_alerts_lang=_parse_pm_alerts_lang(os.getenv("PM_ALERTS_LANG")),
        pm_alerts_file=os.getenv("PM_ALERTS_FILE", f"{session_name}_pm_alerts.json"),
        pm_alerts_exclude_chats=_parse_refs_csv(os.getenv("PM_ALERTS_EXCLUDE_CHATS")),
        pm_alert_require_my_silence=pm_alert_require_my_silence,
        pm_alert_min_silence_after_my_message_minutes=pm_alert_min_silence_after_my_message_minutes,
        pm_alert_my_activity_file=os.getenv("PM_ALERT_MY_ACTIVITY_FILE", f"{session_name}_pm_alerts_my_activity.json"),
        pm_alerts_auto_delete_enabled=pm_alerts_auto_delete_enabled,
        pm_alerts_auto_delete_hour=pm_alerts_auto_delete_hour,
        pm_alerts_auto_delete_minute=pm_alerts_auto_delete_minute,
        pm_alerts_auto_delete_after_hours=pm_alerts_auto_delete_after_hours,
        pm_alerts_auto_delete_file=os.getenv("PM_ALERTS_AUTO_DELETE_FILE", f"{session_name}_pm_alerts_messages.json"),
        email_forwarding_enabled=email_forwarding_enabled,
        email_pm_alerts_enabled=email_pm_alerts_enabled,
        email_smtp_host=email_smtp_host,
        email_smtp_port=email_smtp_port,
        email_use_tls=email_use_tls,
        email_smtp_username=email_smtp_username,
        email_smtp_password=email_smtp_password,
        email_from=email_from,
        email_to=email_to,
    )


def _coerce_ref(chat_ref: str | int) -> str | int:
    if isinstance(chat_ref, int):
        return chat_ref
    try:
        return int(chat_ref)
    except ValueError:
        return chat_ref


async def _resolve_entities(client: TelegramClient, refs: Iterable[str]) -> list[Any]:
    entities: list[Any] = []
    for ref in refs:
        entity = await client.get_entity(_coerce_ref(ref))
        entities.append(entity)
    return entities


def _entity_label(entity: Any) -> str:
    if isinstance(entity, types.User):
        full_name = " ".join(part for part in [entity.first_name, entity.last_name] if part)
        if full_name:
            return full_name
        if entity.username:
            return f"@{entity.username}"
        return str(entity.id)

    title = getattr(entity, "title", None)
    if title:
        return title

    username = getattr(entity, "username", None)
    if username:
        return f"@{username}"

    entity_id = getattr(entity, "id", None)
    return str(entity_id) if entity_id is not None else "Unknown"


def _print_qr(url: str) -> None:
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)
    print()
    if sys.stdout.isatty():
        qr.print_ascii(tty=True, invert=False)
    else:
        qr.print_ascii(invert=False)
    print()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram auto-forward service")
    parser.add_argument(
        "--list-chats",
        action="store_true",
        help="Print available dialogs with IDs and exit",
    )
    parser.add_argument(
        "--list-limit",
        type=int,
        default=200,
        help="How many dialogs to show with --list-chats (default: 200)",
    )
    return parser.parse_args()


def _dialog_type(entity: Any) -> str:
    if isinstance(entity, types.User):
        return "user"
    if isinstance(entity, types.Channel):
        return "supergroup" if entity.megagroup else "channel"
    if isinstance(entity, types.Chat):
        return "group"
    return type(entity).__name__.lower()


async def _list_dialogs(client: TelegramClient, limit: int) -> None:
    print("peer_id | entity_id | type | title")
    print("-" * 90)
    async for dialog in client.iter_dialogs(limit=limit):
        entity = dialog.entity
        peer_id = get_peer_id(entity)
        entity_id = getattr(entity, "id", "n/a")
        chat_type = _dialog_type(entity)
        title = dialog.name or _entity_label(entity)
        print(f"{peer_id} | {entity_id} | {chat_type} | {title}")


async def _resolve_allowed_sender_ids(client: TelegramClient, refs: list[str]) -> set[int]:
    if not refs:
        return set()
    sender_entities = await _resolve_entities(client, refs)
    return {get_peer_id(entity) for entity in sender_entities}


async def _resolve_chat_sender_filters(
    client: TelegramClient,
    raw_filters: dict[str, list[str]],
) -> dict[int, set[int]]:
    resolved: dict[int, set[int]] = {}
    for chat_ref, sender_refs in raw_filters.items():
        chat_entity = await client.get_entity(_coerce_ref(chat_ref))
        chat_peer_id = get_peer_id(chat_entity)
        sender_ids = await _resolve_allowed_sender_ids(client, sender_refs)
        resolved[chat_peer_id] = sender_ids
    return resolved


def _safe_media_filename(message: types.Message) -> str:
    file_meta = getattr(message, "file", None)
    file_name = getattr(file_meta, "name", None) if file_meta else None
    file_ext = getattr(file_meta, "ext", None) if file_meta else None

    if file_name:
        base = os.path.basename(str(file_name).strip())
        if base and base not in {".", ".."}:
            return base

    if not file_ext and file_meta:
        mime_type = getattr(file_meta, "mime_type", None)
        if mime_type:
            file_ext = mimetypes.guess_extension(mime_type) or ""

    if file_ext and not str(file_ext).startswith("."):
        file_ext = f".{file_ext}"

    return f"media{file_ext or ''}"


def _build_message_url(chat_entity: Any, message_id: int) -> str | None:
    if not isinstance(chat_entity, types.Channel):
        return None

    username = getattr(chat_entity, "username", None)
    if username:
        return f"https://t.me/{username}/{message_id}"

    peer_id = get_peer_id(chat_entity)
    abs_peer_id = str(abs(peer_id))
    if abs_peer_id.startswith("100"):
        internal_id = abs_peer_id[3:]
        return f"https://t.me/c/{internal_id}/{message_id}"
    return None


def _format_prefixed_html(
    source_title: str,
    text: str,
    message_url: str | None = None,
    quote_text: str | None = None,
) -> str:
    escaped_prefix = html.escape(f"[{source_title}]")
    prefix_markup = f"<b>{escaped_prefix}</b>"
    if message_url:
        escaped_url = html.escape(message_url, quote=True)
        prefix_markup = f'<a href="{escaped_url}">{prefix_markup}</a>'

    sections: list[str] = [prefix_markup]
    stripped_quote_text = (quote_text or "").strip()
    if stripped_quote_text:
        sections.append(f"<blockquote>{html.escape(stripped_quote_text)}</blockquote>")

    stripped_text = text.strip()
    if stripped_text:
        sections.append(html.escape(stripped_text))

    return "\n\n".join(sections)


def _format_email_forward_plain(
    text: str,
    quote_text: str | None = None,
    message_url: str | None = None,
) -> str:
    sections: list[str] = []
    stripped_quote_text = (quote_text or "").strip()
    if stripped_quote_text:
        quoted_lines = "\n".join(f"> {line}" for line in stripped_quote_text.splitlines())
        sections.append(quoted_lines)

    stripped_text = text.strip()
    if stripped_text:
        sections.append(stripped_text)

    if message_url:
        sections.append(message_url)

    return "\n\n".join(sections)


async def _get_reply_quote_text(message: types.Message) -> str | None:
    reply_to_message_id = getattr(message, "reply_to_msg_id", None)
    if reply_to_message_id is None:
        return None

    try:
        reply_message = await message.get_reply_message()
    except Exception:
        return None
    if reply_message is None:
        return None

    reply_text = (reply_message.message or "").strip()
    if reply_text:
        return reply_text
    if reply_message.media is not None:
        return "[media message]"
    return None


async def _send_media_as_bot(
    source_client: TelegramClient,
    bot_client: TelegramClient,
    bot_target_entity: Any,
    message: types.Message,
    caption: str,
) -> Any:
    with tempfile.TemporaryDirectory(prefix="tgfwd_") as temp_dir:
        file_hint = os.path.join(temp_dir, _safe_media_filename(message))
        downloaded = await _download_media_to_path(source_client, message, file_hint)

        return await bot_client.send_file(
            bot_target_entity,
            file=downloaded,
            caption=caption,
            link_preview=False,
            parse_mode="html",
        )


def _extract_message_id(result: Any) -> int | None:
    message_ids = _extract_message_ids(result)
    return message_ids[0] if message_ids else None


def _extract_message_ids(result: Any) -> list[int]:
    if result is None:
        return []

    if isinstance(result, list):
        ids: list[int] = []
        for item in result:
            message_id = getattr(item, "id", None)
            if isinstance(message_id, int):
                ids.append(message_id)
        return ids

    message_id = getattr(result, "id", None)
    return [message_id] if isinstance(message_id, int) else []


async def _download_media_to_path(
    source_client: TelegramClient,
    message: types.Message,
    file_path: str,
) -> str:
    downloaded = await source_client.download_media(message, file=file_path)
    if not downloaded:
        raise RuntimeError("Failed to download media")

    if isinstance(downloaded, bytes):
        with open(file_path, "wb") as out_file:
            out_file.write(downloaded)
        return file_path

    return str(downloaded)


async def _send_album_as_bot(
    source_client: TelegramClient,
    bot_client: TelegramClient,
    bot_target_entity: Any,
    messages: list[types.Message],
    captions: list[str],
) -> Any:
    with tempfile.TemporaryDirectory(prefix="tgfwd_album_") as temp_dir:
        files: list[str] = []
        for idx, message in enumerate(messages):
            safe_name = _safe_media_filename(message)
            file_hint = os.path.join(temp_dir, f"{idx:02d}_{safe_name}")
            downloaded = await _download_media_to_path(source_client, message, file_hint)
            files.append(downloaded)

        return await bot_client.send_file(
            bot_target_entity,
            file=files,
            caption=captions,
            link_preview=False,
            parse_mode="html",
        )


async def _authorize_client(client: TelegramClient, settings: Settings) -> None:
    if settings.auth_mode == "phone":
        await client.start()
        return

    await client.connect()
    if await client.is_user_authorized():
        return

    logging.info("QR login mode enabled.")
    logging.info("Open Telegram -> Settings -> Devices -> Link Desktop Device, then scan the code below.")

    while True:
        qr_login = await client.qr_login()
        _print_qr(qr_login.url)
        print(f"QR URL (fallback): {qr_login.url}")

        try:
            await qr_login.wait(timeout=120)
            break
        except asyncio.TimeoutError:
            logging.info("QR code expired. Generating a new one...")
        except errors.SessionPasswordNeededError:
            password = getpass("Enter your 2FA password: ")
            await client.sign_in(password=password)
            break


def _chunked(items: list[int], chunk_size: int) -> Iterable[list[int]]:
    for idx in range(0, len(items), chunk_size):
        yield items[idx: idx + chunk_size]


async def _pm_alerts_auto_delete_loop(
    *,
    bot_client: TelegramClient,
    pm_alert_target_entity: Any,
    pm_alert_target_peer_id: int,
    pm_alert_messages_store: PmAlertMessagesStore,
    delete_hour: int,
    delete_minute: int,
    delete_after_hours: int,
) -> None:
    delete_after_seconds = delete_after_hours * 60 * 60
    logging.info(
        "PM alerts auto-delete enabled: daily at %02d:%02d, max age=%dh",
        delete_hour,
        delete_minute,
        delete_after_hours,
    )

    while True:
        now = datetime.now()
        next_run = now.replace(hour=delete_hour, minute=delete_minute, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)

        sleep_seconds = max(1.0, (next_run - now).total_seconds())
        await asyncio.sleep(sleep_seconds)

        cutoff_ts = int(time.time()) - delete_after_seconds
        message_ids = await pm_alert_messages_store.get_expired_ids(pm_alert_target_peer_id, cutoff_ts)
        if not message_ids:
            logging.info("PM alerts auto-delete: no messages to delete")
            continue

        deleted_count = 0
        for batch in _chunked(message_ids, 100):
            try:
                await bot_client.delete_messages(pm_alert_target_entity, batch)
                await pm_alert_messages_store.remove_many(pm_alert_target_peer_id, batch)
                deleted_count += len(batch)
            except Exception as exc:
                logging.exception("PM alerts auto-delete failed for a batch: %s", exc)

        logging.info("PM alerts auto-delete: deleted %s message(s)", deleted_count)


async def main() -> None:
    args = _parse_args()
    settings = load_settings(require_routing=not args.list_chats)
    pm_alerts_active = settings.pm_alerts_enabled or settings.email_pm_alerts_enabled
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    client = TelegramClient(settings.session_name, settings.api_id, settings.api_hash)
    await _authorize_client(client, settings)

    if args.list_chats:
        await _list_dialogs(client, limit=args.list_limit)
        await client.disconnect()
        return

    email_sender: EmailSender | None = None
    if settings.email_forwarding_enabled or settings.email_pm_alerts_enabled:
        email_sender = EmailSender(
            smtp_host=settings.email_smtp_host or "",
            smtp_port=settings.email_smtp_port,
            use_tls=settings.email_use_tls,
            smtp_username=settings.email_smtp_username,
            smtp_password=settings.email_smtp_password,
            from_addr=settings.email_from or "",
            to_addrs=settings.email_to,
        )

    source_entities: list[Any] = []
    target_entity: Any | None = None
    source_delivery_enabled = settings.forwarding_enabled or settings.email_forwarding_enabled
    if source_delivery_enabled:
        source_entities = await _resolve_entities(client, settings.source_chats)
        if settings.forwarding_enabled:
            target_entity = await client.get_entity(settings.target_chat)

    bot_client: TelegramClient | None = None
    bot_target_entity: Any | None = None
    message_map_store: MessageMapStore | None = None
    active_message_map_file: str | None = None
    pm_alert_target_entity: Any | None = None
    pm_alert_target_peer_id: int | None = None
    pm_alerts_store: PmAlertCooldownStore | None = None
    pm_alert_my_activity_store: PmAlertMyActivityStore | None = None
    pm_alert_messages_store: PmAlertMessagesStore | None = None
    pm_alerts_auto_delete_task: asyncio.Task[Any] | None = None
    pm_alert_excluded_chat_ids: set[int] = set()
    need_bot_client = settings.pm_alerts_enabled or (settings.forwarding_enabled and settings.delivery_mode == "bot")
    if need_bot_client:
        bot_client = TelegramClient(f"{settings.session_name}_bot_sender", settings.api_id, settings.api_hash)
        await bot_client.start(bot_token=settings.bot_token)
    if settings.forwarding_enabled and settings.delivery_mode == "bot":
        bot_target_entity = await bot_client.get_entity(settings.bot_target_chat)
    if settings.forwarding_enabled:
        active_message_map_file = (
            settings.message_map_file_bot
            if settings.delivery_mode == "bot"
            else settings.message_map_file_user
        )
        message_map_store = MessageMapStore(
            active_message_map_file,
            ttl_days=settings.message_map_ttl_days,
        )
    if pm_alerts_active:
        if settings.pm_alerts_enabled:
            pm_alert_target_entity = await bot_client.get_entity(settings.pm_alert_target_chat)
            pm_alert_target_peer_id = get_peer_id(pm_alert_target_entity)
        pm_alerts_store = PmAlertCooldownStore(settings.pm_alerts_file)
        if settings.pm_alert_require_my_silence:
            pm_alert_my_activity_store = PmAlertMyActivityStore(settings.pm_alert_my_activity_file)
        if settings.pm_alerts_auto_delete_enabled:
            pm_alert_messages_store = PmAlertMessagesStore(settings.pm_alerts_auto_delete_file)
            pm_alerts_auto_delete_task = asyncio.create_task(
                _pm_alerts_auto_delete_loop(
                    bot_client=bot_client,
                    pm_alert_target_entity=pm_alert_target_entity,
                    pm_alert_target_peer_id=pm_alert_target_peer_id,
                    pm_alert_messages_store=pm_alert_messages_store,
                    delete_hour=settings.pm_alerts_auto_delete_hour,
                    delete_minute=settings.pm_alerts_auto_delete_minute,
                    delete_after_hours=settings.pm_alerts_auto_delete_after_hours,
                )
            )
        if settings.pm_alerts_exclude_chats:
            excluded_entities = await _resolve_entities(client, settings.pm_alerts_exclude_chats)
            pm_alert_excluded_chat_ids = {get_peer_id(entity) for entity in excluded_entities}

    source_peer_ids: set[int] = set()
    target_peer_id: int | None = None
    if source_delivery_enabled:
        source_peer_ids = {get_peer_id(entity) for entity in source_entities}
    if settings.forwarding_enabled:
        try:
            if settings.delivery_mode == "bot":
                target_peer_id = get_peer_id(await client.get_entity(settings.bot_target_chat))
            else:
                target_peer_id = get_peer_id(target_entity)
        except Exception:
            logging.warning("Could not resolve delivery target in user account. Target loop protection may be limited.")

    global_allowed_sender_ids: set[int] = set()
    chat_allowed_sender_ids: dict[int, set[int]] = {}
    if settings.forwarding_enabled:
        global_allowed_sender_ids = await _resolve_allowed_sender_ids(client, settings.allowed_senders)
        chat_allowed_sender_ids = await _resolve_chat_sender_filters(client, settings.chat_allowed_senders)

        if target_peer_id is not None and target_peer_id in source_peer_ids:
            logging.warning("Target chat is also in SOURCE_CHATS. Messages from it will be ignored to avoid loops.")
        for chat_peer_id in chat_allowed_sender_ids:
            if chat_peer_id not in source_peer_ids:
                logging.warning(
                    "CHAT_ALLOWED_SENDERS contains chat %s that is not in SOURCE_CHATS. This filter will not be used.",
                    chat_peer_id,
                )
    elif settings.allowed_senders or settings.chat_allowed_senders:
        logging.warning("Sender filters are configured but FORWARDING_ENABLED=false. They will be ignored.")

    me = await client.get_me()
    logging.info("Connected as %s", me.username or me.id)
    if settings.forwarding_enabled:
        if settings.delivery_mode == "bot":
            bot_me = await bot_client.get_me()
            logging.info("Delivery mode: bot")
            logging.info("Bot sender: %s", bot_me.username or bot_me.id)
            logging.info("Target chat (bot): %s", _entity_label(bot_target_entity))
        else:
            logging.info("Delivery mode: user")
            logging.info("Target chat: %s", _entity_label(target_entity))
        logging.info("Message map file: %s", active_message_map_file)
        logging.info("Source chats: %s", ", ".join(_entity_label(entity) for entity in source_entities))
        if global_allowed_sender_ids:
            logging.info("Global sender filter enabled: %s sender(s)", len(global_allowed_sender_ids))
        if chat_allowed_sender_ids:
            logging.info("Per-chat sender filter enabled for %s chat(s)", len(chat_allowed_sender_ids))
    elif settings.email_forwarding_enabled:
        logging.info("Telegram forwarding to TARGET_CHAT is disabled (FORWARDING_ENABLED=false). Email forwarding is enabled.")
        global_allowed_sender_ids = await _resolve_allowed_sender_ids(client, settings.allowed_senders)
        chat_allowed_sender_ids = await _resolve_chat_sender_filters(client, settings.chat_allowed_senders)
        for chat_peer_id in chat_allowed_sender_ids:
            if chat_peer_id not in source_peer_ids:
                logging.warning(
                    "CHAT_ALLOWED_SENDERS contains chat %s that is not in SOURCE_CHATS. This filter will not be used.",
                    chat_peer_id,
                )
        if global_allowed_sender_ids:
            logging.info("Global sender filter enabled: %s sender(s)", len(global_allowed_sender_ids))
        if chat_allowed_sender_ids:
            logging.info("Per-chat sender filter enabled for %s chat(s)", len(chat_allowed_sender_ids))
        logging.info("Source chats (email): %s", ", ".join(_entity_label(entity) for entity in source_entities))
    else:
        logging.info("Forwarding from SOURCE_CHATS is disabled (FORWARDING_ENABLED=false).")
    if settings.email_forwarding_enabled:
        logging.info("Email forwarding enabled: to=%s", ", ".join(settings.email_to))
    if settings.email_pm_alerts_enabled:
        logging.info("PM alerts email delivery enabled: to=%s", ", ".join(settings.email_to))
    if pm_alerts_active:
        if settings.pm_alerts_enabled:
            logging.info(
                "PM alerts Telegram delivery enabled: target=%s",
                _entity_label(pm_alert_target_entity),
            )
        else:
            logging.info("PM alerts Telegram delivery disabled (PM_ALERTS_ENABLED=false).")
        logging.info(
            "PM alerts enabled: cooldown=%s minute(s), lang=%s",
            settings.pm_alert_cooldown_minutes,
            settings.pm_alerts_lang,
        )
        if settings.pm_alert_require_my_silence:
            logging.info(
                "PM alerts sender silence enabled: min %s minute(s) since your last PM message",
                settings.pm_alert_min_silence_after_my_message_minutes,
            )
        if pm_alert_excluded_chat_ids:
            logging.info(
                "PM alerts exclusions enabled: %s chat(s)",
                len(pm_alert_excluded_chat_ids),
            )
        if settings.pm_alerts_auto_delete_enabled:
            logging.info(
                "PM alerts auto-delete configured: at %02d:%02d, older than %dh, max allowed 48h",
                settings.pm_alerts_auto_delete_hour,
                settings.pm_alerts_auto_delete_minute,
                settings.pm_alerts_auto_delete_after_hours,
            )

    if source_delivery_enabled:
        def _passes_forward_filters(chat_id: int | None, sender_id: int | None, is_out: bool) -> bool:
            if chat_id is None:
                return False

            if settings.skip_outgoing and is_out:
                return False

            if target_peer_id is not None and chat_id == target_peer_id:
                return False

            allowed_for_chat = chat_allowed_sender_ids.get(chat_id)
            if allowed_for_chat is not None:
                return sender_id is not None and sender_id in allowed_for_chat

            if global_allowed_sender_ids:
                return sender_id is not None and sender_id in global_allowed_sender_ids

            return True

        @client.on(events.NewMessage(chats=source_entities))
        async def forward_message(event: events.NewMessage.Event) -> None:
            if not _passes_forward_filters(event.chat_id, event.sender_id, event.out):
                return

            message = event.message
            if message is None or message.action is not None:
                return
            if message.grouped_id is not None:
                # Album messages are handled by events.Album to preserve grouping.
                return

            source = await event.get_chat()
            source_title = _entity_label(source)
            original_text = (message.message or "").strip()
            reply_quote_text = await _get_reply_quote_text(message)
            message_url = _build_message_url(source, message.id)
            formatted_text = _format_prefixed_html(
                source_title,
                original_text,
                message_url=message_url,
                quote_text=reply_quote_text,
            )
            formatted_prefix_only = _format_prefixed_html(source_title, "", message_url=message_url)
            plain_email_text = _format_email_forward_plain(
                original_text,
                quote_text=reply_quote_text,
                message_url=message_url,
            )
            sent_target_message_id: int | None = None
            telegram_sent = False
            email_sent = False

            if settings.forwarding_enabled:
                try:
                    if message.media:
                        caption = formatted_text
                        if settings.delivery_mode == "bot":
                            try:
                                send_result = await _send_media_as_bot(
                                    source_client=client,
                                    bot_client=bot_client,
                                    bot_target_entity=bot_target_entity,
                                    message=message,
                                    caption=caption,
                                )
                                sent_target_message_id = _extract_message_id(send_result)
                            except Exception:
                                sent_msg = await bot_client.send_message(
                                    bot_target_entity,
                                    formatted_prefix_only,
                                    link_preview=False,
                                    parse_mode="html",
                                )
                                sent_target_message_id = _extract_message_id(sent_msg)
                        else:
                            try:
                                sent_msg = await client.send_file(
                                    target_entity,
                                    file=message.media,
                                    caption=caption,
                                    link_preview=False,
                                    parse_mode="html",
                                )
                                sent_target_message_id = _extract_message_id(sent_msg)
                            except Exception:
                                # Some media types may not allow captions.
                                sent_msg = await client.send_message(
                                    target_entity,
                                    formatted_prefix_only,
                                    link_preview=False,
                                    parse_mode="html",
                                )
                                sent_target_message_id = _extract_message_id(sent_msg)
                                await client.forward_messages(target_entity, message)
                    else:
                        if not original_text:
                            # Skip text-only forwarding to Telegram if there is no text.
                            pass
                        elif settings.delivery_mode == "bot":
                            sent_msg = await bot_client.send_message(
                                bot_target_entity,
                                formatted_text,
                                link_preview=False,
                                parse_mode="html",
                            )
                            sent_target_message_id = _extract_message_id(sent_msg)
                        else:
                            sent_msg = await client.send_message(
                                target_entity,
                                formatted_text,
                                link_preview=False,
                                parse_mode="html",
                            )
                            sent_target_message_id = _extract_message_id(sent_msg)

                    if (
                        message_map_store is not None
                        and sent_target_message_id is not None
                        and event.chat_id is not None
                    ):
                        await message_map_store.set(event.chat_id, message.id, sent_target_message_id)

                    if settings.delivery_mode == "user" and sent_target_message_id is not None:
                        await client(functions.messages.MarkDialogUnreadRequest(peer=target_entity, unread=True))
                    telegram_sent = sent_target_message_id is not None
                except Exception as exc:
                    logging.exception("Failed Telegram forwarding from %s: %s", source_title, exc)

            if settings.email_forwarding_enabled and email_sender is not None:
                try:
                    attachments: list[tuple[str, str]] = []
                    with tempfile.TemporaryDirectory(prefix="tgfwd_email_") as temp_dir:
                        if message.media:
                            file_hint = os.path.join(temp_dir, _safe_media_filename(message))
                            downloaded = await _download_media_to_path(client, message, file_hint)
                            attachments.append((downloaded, os.path.basename(downloaded)))
                        elif not original_text and not reply_quote_text:
                            return

                        await email_sender.send(
                            subject=source_title,
                            body=plain_email_text,
                            attachments=attachments,
                        )
                    email_sent = True
                except Exception as exc:
                    logging.exception("Failed email forwarding from %s: %s", source_title, exc)

            if telegram_sent or email_sent:
                logging.info(
                    "Forwarded message from %s%s%s",
                    source_title,
                    " [telegram]" if telegram_sent else "",
                    " [email]" if email_sent else "",
                )

        @client.on(events.Album(chats=source_entities))
        async def forward_album(event: events.Album.Event) -> None:
            if not _passes_forward_filters(event.chat_id, event.sender_id, event.out):
                return

            album_messages = [
                message
                for message in event.messages
                if message is not None and message.media and message.action is None
            ]
            if not album_messages:
                return

            source = await event.get_chat()
            source_title = _entity_label(source)
            first_message_url = _build_message_url(source, album_messages[0].id)
            first_reply_quote_text = await _get_reply_quote_text(album_messages[0])

            captions: list[str] = []
            for idx, message in enumerate(album_messages):
                text = (message.message or "").strip()
                if idx == 0:
                    captions.append(
                        _format_prefixed_html(
                            source_title,
                            text,
                            message_url=first_message_url,
                            quote_text=first_reply_quote_text,
                        )
                    )
                else:
                    captions.append(html.escape(text) if text else "")

            sent_target_ids: list[int] = []
            telegram_sent = False
            email_sent = False
            if settings.forwarding_enabled:
                try:
                    if settings.delivery_mode == "bot":
                        try:
                            send_result = await _send_album_as_bot(
                                source_client=client,
                                bot_client=bot_client,
                                bot_target_entity=bot_target_entity,
                                messages=album_messages,
                                captions=captions,
                            )
                            sent_target_ids = _extract_message_ids(send_result)
                        except Exception:
                            logging.exception(
                                "Failed to send album as grouped media from %s; falling back to separate messages.",
                                source_title,
                            )
                            for idx, message in enumerate(album_messages):
                                try:
                                    send_result = await _send_media_as_bot(
                                        source_client=client,
                                        bot_client=bot_client,
                                        bot_target_entity=bot_target_entity,
                                        message=message,
                                        caption=captions[idx],
                                    )
                                    sent_message_id = _extract_message_id(send_result)
                                    if sent_message_id is not None:
                                        sent_target_ids.append(sent_message_id)
                                except Exception:
                                    logging.exception(
                                        "Failed to send album item %s from %s",
                                        idx + 1,
                                        source_title,
                                    )
                    else:
                        try:
                            send_result = await client.send_file(
                                target_entity,
                                file=[message.media for message in album_messages],
                                caption=captions,
                                link_preview=False,
                                parse_mode="html",
                            )
                            sent_target_ids = _extract_message_ids(send_result)
                        except Exception:
                            logging.exception(
                                "Failed to send album as grouped media from %s; falling back to separate messages.",
                                source_title,
                            )
                            for idx, message in enumerate(album_messages):
                                try:
                                    send_result = await client.send_file(
                                        target_entity,
                                        file=message.media,
                                        caption=captions[idx],
                                        link_preview=False,
                                        parse_mode="html",
                                    )
                                    sent_message_id = _extract_message_id(send_result)
                                    if sent_message_id is not None:
                                        sent_target_ids.append(sent_message_id)
                                except Exception:
                                    logging.exception(
                                        "Failed to send album item %s from %s",
                                        idx + 1,
                                        source_title,
                                    )

                    if (
                        message_map_store is not None
                        and event.chat_id is not None
                        and sent_target_ids
                    ):
                        for source_message, target_message_id in zip(album_messages, sent_target_ids):
                            await message_map_store.set(event.chat_id, source_message.id, target_message_id)
                    telegram_sent = bool(sent_target_ids)
                except Exception as exc:
                    logging.exception("Failed Telegram album forwarding from %s: %s", source_title, exc)

            if settings.email_forwarding_enabled and email_sender is not None:
                try:
                    body_parts: list[str] = []
                    body_parts.append(
                        _format_email_forward_plain(
                            (album_messages[0].message or "").strip(),
                            quote_text=first_reply_quote_text,
                            message_url=first_message_url,
                        )
                    )
                    for idx, album_message in enumerate(album_messages[1:], start=2):
                        extra_text = (album_message.message or "").strip()
                        if extra_text:
                            body_parts.append(f"[item {idx}]\n{extra_text}")
                    album_body = "\n\n".join(part for part in body_parts if part.strip())

                    with tempfile.TemporaryDirectory(prefix="tgfwd_email_album_") as temp_dir:
                        attachments: list[tuple[str, str]] = []
                        for idx, album_message in enumerate(album_messages):
                            safe_name = _safe_media_filename(album_message)
                            file_hint = os.path.join(temp_dir, f"{idx:02d}_{safe_name}")
                            downloaded = await _download_media_to_path(client, album_message, file_hint)
                            attachments.append((downloaded, os.path.basename(downloaded)))

                        await email_sender.send(
                            subject=source_title,
                            body=album_body,
                            attachments=attachments,
                        )
                    email_sent = True
                except Exception as exc:
                    logging.exception("Failed email album forwarding from %s: %s", source_title, exc)

            if telegram_sent or email_sent:
                logging.info(
                    "Forwarded album from %s%s%s",
                    source_title,
                    " [telegram]" if telegram_sent else "",
                    " [email]" if email_sent else "",
                )

    @client.on(events.NewMessage(outgoing=True))
    async def pm_my_activity_handler(event: events.NewMessage.Event) -> None:
        if not pm_alerts_active or not settings.pm_alert_require_my_silence:
            return
        if pm_alert_my_activity_store is None:
            return
        if not event.is_private:
            return

        message = event.message
        if message is None or message.action is not None:
            return
        if event.chat_id is None:
            return

        await pm_alert_my_activity_store.touch_my_message(event.chat_id)

    @client.on(events.NewMessage(incoming=True))
    async def pm_alerts_handler(event: events.NewMessage.Event) -> None:
        if not pm_alerts_active or pm_alerts_store is None:
            return
        if not event.is_private or event.out:
            return
        if (
            event.chat_id is not None
            and event.chat_id in pm_alert_excluded_chat_ids
        ):
            return
        if (
            event.sender_id is not None
            and event.sender_id in pm_alert_excluded_chat_ids
        ):
            return

        message = event.message
        if message is None or message.action is not None:
            return
        if event.sender_id is None:
            return

        if settings.pm_alert_require_my_silence and pm_alert_my_activity_store is not None:
            peer_for_silence = event.chat_id or event.sender_id
            if peer_for_silence is None:
                return
            min_silence_seconds = settings.pm_alert_min_silence_after_my_message_minutes * 60
            has_silence = await pm_alert_my_activity_store.has_required_silence(peer_for_silence, min_silence_seconds)
            if not has_silence:
                return
        cooldown_seconds = settings.pm_alert_cooldown_minutes * 60
        should_notify = await pm_alerts_store.should_notify(event.sender_id, cooldown_seconds)
        if not should_notify:
            return

        sender = await event.get_sender()
        sender_label = _entity_label(sender)
        if settings.pm_alerts_lang == "eng":
            alert_text = f"{sender_label} sent a new message"
            email_alert_text = "Sent a new message"
        else:
            alert_text = f"{sender_label} отправил(-а) новое сообщение"
            email_alert_text = "Отправил(-а) новое сообщение"
        telegram_sent = False
        email_sent = False

        if settings.pm_alerts_enabled:
            try:
                sent_message = await bot_client.send_message(pm_alert_target_entity, alert_text, link_preview=False)
                sent_message_id = _extract_message_id(sent_message)
                if (
                    settings.pm_alerts_auto_delete_enabled
                    and pm_alert_messages_store is not None
                    and pm_alert_target_peer_id is not None
                    and sent_message_id is not None
                ):
                    await pm_alert_messages_store.add(pm_alert_target_peer_id, sent_message_id)
                telegram_sent = True
            except Exception as exc:
                logging.exception("Failed to send PM alert to Telegram for %s: %s", sender_label, exc)

        if settings.email_pm_alerts_enabled and email_sender is not None:
            try:
                await email_sender.send(
                    subject=sender_label,
                    body=email_alert_text,
                )
                email_sent = True
            except Exception as exc:
                logging.exception("Failed to send PM alert email for %s: %s", sender_label, exc)

        if telegram_sent or email_sent:
            logging.info(
                "Sent PM alert for %s%s%s",
                sender_label,
                " [telegram]" if telegram_sent else "",
                " [email]" if email_sent else "",
            )

    if settings.forwarding_enabled:
        @client.on(events.MessageEdited(chats=source_entities))
        async def edit_forwarded_message(event: events.MessageEdited.Event) -> None:
            if message_map_store is None:
                return

            if not _passes_forward_filters(event.chat_id, event.sender_id, event.out):
                return

            message = event.message
            if message is None or message.action is not None or event.chat_id is None:
                return

            mapped_target_message_id = await message_map_store.get(event.chat_id, message.id)
            if mapped_target_message_id is None:
                return

            source = await event.get_chat()
            source_title = _entity_label(source)
            original_text = (message.message or "").strip()
            reply_quote_text = await _get_reply_quote_text(message)
            message_url = _build_message_url(source, message.id)
            formatted_text = _format_prefixed_html(
                source_title,
                original_text,
                message_url=message_url,
                quote_text=reply_quote_text,
            )

            try:
                if settings.delivery_mode == "bot":
                    await bot_client.edit_message(
                        bot_target_entity,
                        mapped_target_message_id,
                        formatted_text,
                        link_preview=False,
                        parse_mode="html",
                    )
                else:
                    await client.edit_message(
                        target_entity,
                        mapped_target_message_id,
                        formatted_text,
                        link_preview=False,
                        parse_mode="html",
                    )
                logging.info("Edited forwarded message from %s", source_title)
            except errors.MessageNotModifiedError:
                pass
            except Exception as exc:
                logging.exception("Failed to edit forwarded message from %s: %s", source_title, exc)

    logging.info("Forwarder is running. Press Ctrl+C to stop.")
    try:
        await client.run_until_disconnected()
    finally:
        if pm_alerts_auto_delete_task is not None:
            pm_alerts_auto_delete_task.cancel()
            with suppress(asyncio.CancelledError):
                await pm_alerts_auto_delete_task


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
