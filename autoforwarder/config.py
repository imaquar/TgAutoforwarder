from dataclasses import dataclass
import json
import os

from dotenv import load_dotenv


@dataclass
class Settings:
    api_id: int
    api_hash: str
    session_name: str
    forwarding_enabled: bool
    source_chats: list[str]
    target_chat: str | int | None
    bot_token: str | None
    bot_target_chat: str | int | None
    auth_mode: str
    skip_outgoing: bool
    allowed_senders: list[str]
    chat_allowed_senders: dict[str, list[str]]
    message_map_file_bot: str
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
    pm_alert_sync_target_read_state_enabled: bool
    pm_alert_sync_target_read_state_file: str
    pm_alert_sync_target_read_state_check_seconds: int
    pm_alert_deferred_unread_enabled: bool
    pm_alert_deferred_unread_minutes: int
    pm_alert_deferred_unread_file: str
    email_forwarding_enabled: bool
    email_pm_alerts_batch_enabled: bool
    email_pm_alerts_batch_minutes: int
    email_pm_alerts_batch_file: str
    email_smtp_host: str | None
    email_smtp_port: int
    email_use_tls: bool
    email_smtp_username: str | None
    email_smtp_password: str | None
    email_from: str | None
    email_to: list[str]


def _parse_bool(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_auth_mode(value: str | None) -> str:
    mode = (value or "phone").strip().lower()
    if mode not in {"phone", "qr"}:
        raise ValueError("AUTH_MODE must be either 'phone' or 'qr'")
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


def _coerce_ref(chat_ref: str | int) -> str | int:
    if isinstance(chat_ref, int):
        return chat_ref
    try:
        return int(chat_ref)
    except ValueError:
        return chat_ref


def load_settings(require_routing: bool = True) -> Settings:
    load_dotenv()

    api_id_raw = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    source_chats_raw = os.getenv("SOURCE_CHATS", "")
    target_chat = os.getenv("TARGET_CHAT", "")
    forwarding_enabled = _parse_bool(os.getenv("FORWARDING_ENABLED"), default=True)
    bot_token = (os.getenv("BOT_TOKEN") or "").strip() or None
    bot_target_chat_raw = (os.getenv("BOT_TARGET_CHAT") or "").strip()
    pm_alerts_enabled = _parse_bool(os.getenv("PM_ALERTS_ENABLED"), default=False)
    pm_alert_target_chat_raw = (os.getenv("PM_ALERT_TARGET_CHAT") or "").strip()
    pm_alert_require_my_silence = _parse_bool(os.getenv("PM_ALERT_REQUIRE_MY_SILENCE"), default=False)
    pm_alerts_auto_delete_enabled = _parse_bool(os.getenv("PM_ALERTS_AUTO_DELETE_ENABLED"), default=False)
    pm_alert_sync_target_read_state_enabled = _parse_bool(
        os.getenv("PM_ALERTS_SYNC_TARGET_READ_STATE_ENABLED"),
        default=False,
    )
    pm_alert_deferred_unread_enabled = _parse_bool(os.getenv("PM_ALERT_DEFERRED_UNREAD_ENABLED"), default=False)
    email_forwarding_enabled = _parse_bool(os.getenv("EMAIL_FORWARDING_ENABLED"), default=False)
    email_pm_alerts_batch_enabled = _parse_bool(os.getenv("EMAIL_PM_ALERTS_BATCH_ENABLED"), default=False)
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
    pm_alert_deferred_unread_minutes = _parse_non_negative_int(
        os.getenv("PM_ALERT_DEFERRED_UNREAD_MINUTES"),
        default=10,
        var_name="PM_ALERT_DEFERRED_UNREAD_MINUTES",
    )
    pm_alert_sync_target_read_state_check_seconds = _parse_non_negative_int(
        os.getenv("PM_ALERTS_SYNC_TARGET_READ_STATE_CHECK_SECONDS"),
        default=10,
        var_name="PM_ALERTS_SYNC_TARGET_READ_STATE_CHECK_SECONDS",
    )
    email_pm_alerts_batch_minutes = _parse_non_negative_int(
        os.getenv("EMAIL_PM_ALERTS_BATCH_MINUTES"),
        default=10,
        var_name="EMAIL_PM_ALERTS_BATCH_MINUTES",
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
    pm_alerts_active = pm_alerts_enabled or email_pm_alerts_batch_enabled
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
    if pm_alert_sync_target_read_state_enabled and not pm_alerts_enabled:
        raise ValueError("PM_ALERTS_SYNC_TARGET_READ_STATE_ENABLED=true requires PM_ALERTS_ENABLED=true")
    if pm_alert_deferred_unread_enabled and not pm_alerts_enabled:
        raise ValueError("PM_ALERT_DEFERRED_UNREAD_ENABLED=true requires PM_ALERTS_ENABLED=true")
    if pm_alert_require_my_silence and not pm_alerts_active:
        raise ValueError("PM_ALERT_REQUIRE_MY_SILENCE=true requires PM alerts delivery enabled")
    if email_pm_alerts_batch_enabled and email_pm_alerts_batch_minutes < 1:
        raise ValueError("EMAIL_PM_ALERTS_BATCH_MINUTES must be at least 1 when EMAIL_PM_ALERTS_BATCH_ENABLED=true")

    if pm_alerts_auto_delete_after_hours > 48:
        raise ValueError("PM_ALERTS_AUTO_DELETE_AFTER_HOURS cannot be greater than 48")
    if pm_alerts_auto_delete_enabled and pm_alerts_auto_delete_after_hours < 1:
        raise ValueError("PM_ALERTS_AUTO_DELETE_AFTER_HOURS must be at least 1 when PM alerts auto-delete is enabled")
    if pm_alert_deferred_unread_enabled and pm_alert_deferred_unread_minutes < 1:
        raise ValueError("PM_ALERT_DEFERRED_UNREAD_MINUTES must be at least 1 when deferred unread alerts are enabled")
    if (
        pm_alert_sync_target_read_state_enabled
        and pm_alert_sync_target_read_state_check_seconds < 1
    ):
        raise ValueError(
            "PM_ALERTS_SYNC_TARGET_READ_STATE_CHECK_SECONDS must be at least 1 "
            "when target read-state sync is enabled"
        )

    if forwarding_enabled or pm_alerts_enabled:
        if not bot_token:
            raise ValueError(
                "Environment variable BOT_TOKEN is required when forwarding is enabled "
                "or when PM alerts Telegram delivery is enabled (PM_ALERTS_ENABLED=true)"
            )

    if email_smtp_port < 1:
        raise ValueError("EMAIL_SMTP_PORT must be a positive integer")

    email_any_enabled = email_forwarding_enabled or email_pm_alerts_batch_enabled
    if email_any_enabled:
        if not email_smtp_host:
            raise ValueError("EMAIL_SMTP_HOST is required when email delivery is enabled")
        if not email_from:
            raise ValueError("EMAIL_FROM is required when email delivery is enabled")
        if not email_to:
            raise ValueError("EMAIL_TO is required when email delivery is enabled")
        if email_smtp_username and not email_smtp_password:
            raise ValueError("EMAIL_SMTP_PASSWORD is required when EMAIL_SMTP_USERNAME is set")

    if forwarding_enabled:
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
        bot_token=bot_token,
        bot_target_chat=bot_target_chat_ref,
        auth_mode=_parse_auth_mode(os.getenv("AUTH_MODE")),
        skip_outgoing=_parse_bool(os.getenv("SKIP_OUTGOING"), default=True),
        allowed_senders=_parse_refs_csv(os.getenv("ALLOWED_SENDERS")),
        chat_allowed_senders=_parse_chat_allowed_senders(os.getenv("CHAT_ALLOWED_SENDERS")),
        message_map_file_bot=(os.getenv("MESSAGE_MAP_FILE_BOT") or "").strip() or default_message_map_file_bot,
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
        pm_alert_sync_target_read_state_enabled=pm_alert_sync_target_read_state_enabled,
        pm_alert_sync_target_read_state_file=os.getenv(
            "PM_ALERTS_SYNC_TARGET_READ_STATE_FILE",
            f"{session_name}_pm_alerts_read_sync.json",
        ),
        pm_alert_sync_target_read_state_check_seconds=pm_alert_sync_target_read_state_check_seconds,
        pm_alert_deferred_unread_enabled=pm_alert_deferred_unread_enabled,
        pm_alert_deferred_unread_minutes=pm_alert_deferred_unread_minutes,
        pm_alert_deferred_unread_file=os.getenv(
            "PM_ALERT_DEFERRED_UNREAD_FILE",
            f"{session_name}_pm_alerts_deferred_unread.json",
        ),
        email_forwarding_enabled=email_forwarding_enabled,
        email_pm_alerts_batch_enabled=email_pm_alerts_batch_enabled,
        email_pm_alerts_batch_minutes=email_pm_alerts_batch_minutes,
        email_pm_alerts_batch_file=os.getenv(
            "EMAIL_PM_ALERTS_BATCH_FILE",
            f"{session_name}_email_pm_alerts_batch.json",
        ),
        email_smtp_host=email_smtp_host,
        email_smtp_port=email_smtp_port,
        email_use_tls=email_use_tls,
        email_smtp_username=email_smtp_username,
        email_smtp_password=email_smtp_password,
        email_from=email_from,
        email_to=email_to,
    )
