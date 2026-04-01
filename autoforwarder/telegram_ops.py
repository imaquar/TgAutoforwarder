import asyncio
from datetime import datetime, timedelta
from getpass import getpass
import html
import logging
import os
import tempfile
import time
from typing import Any, Iterable

import qrcode
from telethon import TelegramClient, errors, functions, types
from telethon.tl.custom import Dialog
from telethon.utils import get_peer_id

from .config import Settings
from .emailer import EmailSender
from .stores import (
    PmAlertCooldownStore,
    PmAlertDeferredStore,
    PmAlertMessagesStore,
    PmAlertMyActivityStore,
    PmAlertReadSyncStore,
    EmailPmBatchStore,
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


def _format_pm_alert_email_item(message: types.Message) -> tuple[str, bool]:
    raw_text = (message.message or "").strip()
    normalized_text = " ".join(raw_text.splitlines()).strip() if raw_text else ""

    is_sticker = bool(getattr(message, "sticker", False))
    is_voice = bool(getattr(message, "voice", False))
    is_video_note = bool(getattr(message, "video_note", False))
    has_photo = getattr(message, "photo", None) is not None
    has_document = getattr(message, "document", None) is not None

    if is_sticker:
        return ("[sticker]", False)
    if is_voice or is_video_note:
        return ("[voice message]", False)
    if has_photo or has_document:
        return ("[file]", False)
    if message.media is not None:
        return ("[file]", False)
    if normalized_text:
        return (normalized_text, False)
    return ("[empty message]", False)


def _build_pm_alert_text(sender_label: str, lang: str) -> str:
    if lang == "eng":
        return f"{sender_label} sent a new message"
    return f"{sender_label} отправил(-а) новое сообщение"


async def _should_send_telegram_pm_alert(
    *,
    settings: Settings,
    pm_alerts_store: PmAlertCooldownStore,
    sender_id: int,
    now_ts: int,
    last_my_message_ts: int | None,
) -> bool:
    cooldown_seconds = settings.pm_alert_cooldown_minutes * 60
    if cooldown_seconds <= 0:
        return True

    last_alert_ts = await pm_alerts_store.get_last_alert_ts(sender_id)
    if last_alert_ts is None:
        return True

    # Unified behavior: if you replied after previous alert,
    # start a new dialog cycle and ignore old sender cooldown.
    if last_my_message_ts is not None and last_my_message_ts > last_alert_ts:
        return True
    return (now_ts - last_alert_ts) >= cooldown_seconds


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


async def _send_telegram_pm_alert(
    *,
    bot_client: TelegramClient,
    pm_alert_target_entity: Any,
    pm_alert_target_peer_id: int | None,
    pm_alert_messages_store: PmAlertMessagesStore | None,
    pm_alert_read_sync_store: PmAlertReadSyncStore | None,
    pm_alerts_store: PmAlertCooldownStore,
    settings: Settings,
    sender_id: int,
    sender_label: str,
    alert_text: str,
    source_peer_id: int | None = None,
    source_message_id: int | None = None,
) -> bool:
    cooldown_seconds = settings.pm_alert_cooldown_minutes * 60
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
        if (
            settings.pm_alert_sync_target_read_state_enabled
            and pm_alert_read_sync_store is not None
            and pm_alert_target_peer_id is not None
            and sent_message_id is not None
        ):
            await pm_alert_read_sync_store.add(
                pm_alert_target_peer_id,
                sent_message_id,
                source_peer_id=source_peer_id,
                source_message_id=source_message_id,
            )
        await pm_alerts_store.touch_alert(sender_id, cooldown_seconds)
        return True
    except Exception as exc:
        logging.exception("Failed to send PM alert to Telegram for %s: %s", sender_label, exc)
        return False


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


async def _pm_alerts_deferred_unread_loop(
    *,
    client: TelegramClient,
    bot_client: TelegramClient,
    settings: Settings,
    pm_alert_target_entity: Any,
    pm_alert_target_peer_id: int | None,
    pm_alerts_store: PmAlertCooldownStore,
    pm_alert_messages_store: PmAlertMessagesStore | None,
    pm_alert_read_sync_store: PmAlertReadSyncStore | None,
    pm_alert_my_activity_store: PmAlertMyActivityStore | None,
    deferred_store: PmAlertDeferredStore,
) -> None:
    idle_sleep_seconds = 5
    logging.info(
        "Deferred unread PM alerts enabled: %s minute(s), file=%s",
        settings.pm_alert_deferred_unread_minutes,
        settings.pm_alert_deferred_unread_file,
    )

    while True:
        now_ts = int(time.time())
        due_entries = await deferred_store.get_due_entries(now_ts)
        if due_entries:
            for sender_id, peer_id, message_id, sender_label in due_entries:
                try:
                    message = await client.get_messages(peer_id, ids=message_id)
                    if isinstance(message, list):
                        message = message[0] if message else None
                    if message is None:
                        await deferred_store.remove(sender_id)
                        continue

                    if not bool(getattr(message, "unread", False)):
                        await deferred_store.remove(sender_id)
                        continue

                    last_my_message_ts: int | None = None
                    if settings.pm_alert_require_my_silence and pm_alert_my_activity_store is not None:
                        min_silence_seconds = settings.pm_alert_min_silence_after_my_message_minutes * 60
                        last_my_message_ts = await pm_alert_my_activity_store.get_last_my_message_ts(peer_id)
                        if (
                            last_my_message_ts is not None
                            and (now_ts - last_my_message_ts) < min_silence_seconds
                        ):
                            next_due = now_ts + settings.pm_alert_deferred_unread_minutes * 60
                            await deferred_store.upsert(
                                sender_id=sender_id,
                                peer_id=peer_id,
                                message_id=message_id,
                                sender_label=sender_label,
                                due_at=next_due,
                            )
                            continue

                    should_notify = await _should_send_telegram_pm_alert(
                        settings=settings,
                        pm_alerts_store=pm_alerts_store,
                        sender_id=sender_id,
                        now_ts=now_ts,
                        last_my_message_ts=last_my_message_ts,
                    )
                    if not should_notify:
                        next_due = now_ts + max(60, settings.pm_alert_cooldown_minutes * 60)
                        await deferred_store.upsert(
                            sender_id=sender_id,
                            peer_id=peer_id,
                            message_id=message_id,
                            sender_label=sender_label,
                            due_at=next_due,
                        )
                        continue

                    alert_text = _build_pm_alert_text(sender_label, settings.pm_alerts_lang)
                    sent = await _send_telegram_pm_alert(
                        bot_client=bot_client,
                        pm_alert_target_entity=pm_alert_target_entity,
                        pm_alert_target_peer_id=pm_alert_target_peer_id,
                        pm_alert_messages_store=pm_alert_messages_store,
                        pm_alert_read_sync_store=pm_alert_read_sync_store,
                        pm_alerts_store=pm_alerts_store,
                        settings=settings,
                        sender_id=sender_id,
                        sender_label=sender_label,
                        alert_text=alert_text,
                        source_peer_id=peer_id,
                        source_message_id=message_id,
                    )
                    if sent:
                        await deferred_store.remove(sender_id)
                        logging.info("Sent deferred unread PM alert for %s", sender_label)
                except Exception as exc:
                    logging.exception("Deferred unread PM alerts loop failed for %s: %s", sender_label, exc)
            continue

        next_due_ts = await deferred_store.next_due_ts()
        if next_due_ts is None:
            await asyncio.sleep(idle_sleep_seconds)
            continue

        sleep_seconds = max(1, min(idle_sleep_seconds, next_due_ts - int(time.time())))
        await asyncio.sleep(sleep_seconds)


async def _pm_alerts_sync_target_read_state_loop(
    *,
    client: TelegramClient,
    pm_alert_target_entity_user: Any,
    pm_alert_target_peer_id: int,
    read_sync_store: PmAlertReadSyncStore,
    check_seconds: int,
) -> None:
    logging.info(
        "PM alerts target read-state sync enabled: check every %ss",
        check_seconds,
    )

    while True:
        try:
            pending_entries = await read_sync_store.list_entries(pm_alert_target_peer_id)
            if not pending_entries:
                await asyncio.sleep(check_seconds)
                continue

            resolved_ids: list[int] = []
            for alert_message_id, source_peer_id, source_message_id in pending_entries:
                if source_peer_id is None or source_message_id is None:
                    # Legacy records from old format; drop them.
                    resolved_ids.append(alert_message_id)
                    continue
                try:
                    source_input_peer = await client.get_input_entity(source_peer_id)
                    peer_dialogs = await client(
                        functions.messages.GetPeerDialogsRequest(peers=[source_input_peer])
                    )
                    source_dialog = peer_dialogs.dialogs[0] if peer_dialogs.dialogs else None
                    if source_dialog is None:
                        continue
                    read_inbox_max_id = int(getattr(source_dialog, "read_inbox_max_id", 0) or 0)
                    if source_message_id <= read_inbox_max_id:
                        resolved_ids.append(alert_message_id)
                        continue
                except (ValueError, errors.PeerIdInvalidError):
                    # Entity may be temporarily unavailable in cache after restart.
                    # Keep the record and retry on the next loop to avoid false "read" marks.
                    continue
                except Exception as exc:
                    logging.exception(
                        "Failed to check source PM read state for %s/%s: %s",
                        source_peer_id,
                        source_message_id,
                        exc,
                    )

            if resolved_ids:
                await read_sync_store.remove_many(pm_alert_target_peer_id, resolved_ids)

            remaining_count = await read_sync_store.count(pm_alert_target_peer_id)
            if remaining_count == 0:
                try:
                    await client.send_read_acknowledge(pm_alert_target_entity_user, clear_mentions=True)
                except Exception as exc:
                    logging.exception("Failed to send read acknowledge for PM alerts target chat: %s", exc)
                try:
                    await client(
                        functions.messages.MarkDialogUnreadRequest(
                            peer=pm_alert_target_entity_user,
                            unread=False,
                        )
                    )
                    logging.info("Marked PM alerts target chat as read after linked source PM messages were read")
                except Exception as exc:
                    logging.exception("Failed to mark PM alerts target chat as read: %s", exc)

            await asyncio.sleep(check_seconds)
        except Exception as exc:
            logging.exception("PM alerts target read-state sync loop failed: %s", exc)
            await asyncio.sleep(check_seconds)


async def _email_pm_alerts_batch_loop(
    *,
    email_sender: EmailSender,
    batch_store: EmailPmBatchStore,
) -> None:
    retry_delay_seconds = 60
    idle_sleep_seconds = 5
    logging.info("Email PM alerts batch loop is running.")

    while True:
        now_ts = int(time.time())
        due_entries = await batch_store.get_due_entries(now_ts)
        if due_entries:
            for sender_id, sender_label, items in due_entries:
                body_lines = [str(item["line"]) for item in items]
                body = "\n".join(body_lines)
                try:
                    await email_sender.send(
                        subject=sender_label,
                        body=body,
                    )
                    await batch_store.remove(sender_id)
                    logging.info(
                        "Sent batched PM alert email for %s (%s lines)",
                        sender_label,
                        len(body_lines),
                    )
                except Exception as exc:
                    logging.exception(
                        "Failed to send batched PM alert email for %s: %s",
                        sender_label,
                        exc,
                    )
                    await batch_store.postpone(sender_id, retry_delay_seconds)
            continue

        next_due_ts = await batch_store.next_due_ts()
        if next_due_ts is None:
            await asyncio.sleep(idle_sleep_seconds)
            continue

        sleep_seconds = max(1, min(idle_sleep_seconds, next_due_ts - int(time.time())))
        await asyncio.sleep(sleep_seconds)
