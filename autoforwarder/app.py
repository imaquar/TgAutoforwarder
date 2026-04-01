import argparse
import asyncio
from contextlib import suppress
from datetime import datetime
import html
import logging
import os
import tempfile
import time
from typing import Any

from telethon import TelegramClient, errors, events
from telethon.sessions import MemorySession
from telethon.utils import get_peer_id

from .config import load_settings
from .emailer import EmailSender
from .stores import (
    EmailPmBatchStore,
    MessageMapStore,
    PmAlertCooldownStore,
    PmAlertDeferredStore,
    PmAlertMessagesStore,
    PmAlertMyActivityStore,
    PmAlertReadSyncStore,
)
from .telegram_ops import (
    _authorize_client,
    _build_message_url,
    _build_pm_alert_text,
    _download_media_to_path,
    _email_pm_alerts_batch_loop,
    _entity_label,
    _extract_message_id,
    _extract_message_ids,
    _format_email_forward_plain,
    _format_pm_alert_email_item,
    _format_prefixed_html,
    _get_reply_quote_text,
    _list_dialogs,
    _pm_alerts_auto_delete_loop,
    _pm_alerts_deferred_unread_loop,
    _pm_alerts_sync_target_read_state_loop,
    _resolve_allowed_sender_ids,
    _resolve_chat_sender_filters,
    _resolve_entities,
    _safe_media_filename,
    _send_album_as_bot,
    _send_media_as_bot,
    _send_telegram_pm_alert,
    _should_send_telegram_pm_alert,
)



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


async def main() -> None:
    args = _parse_args()
    settings = load_settings(require_routing=not args.list_chats)
    pm_alerts_active = settings.pm_alerts_enabled or settings.email_pm_alerts_batch_enabled
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
    if settings.email_forwarding_enabled or settings.email_pm_alerts_batch_enabled:
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
    source_delivery_enabled = settings.forwarding_enabled or settings.email_forwarding_enabled
    if source_delivery_enabled:
        source_entities = await _resolve_entities(client, settings.source_chats)

    bot_client: TelegramClient | None = None
    bot_target_entity: Any | None = None
    message_map_store: MessageMapStore | None = None
    active_message_map_file: str | None = None
    pm_alert_target_entity: Any | None = None
    pm_alert_target_entity_user: Any | None = None
    pm_alert_target_peer_id: int | None = None
    pm_alerts_store: PmAlertCooldownStore | None = None
    pm_alert_my_activity_store: PmAlertMyActivityStore | None = None
    pm_alert_messages_store: PmAlertMessagesStore | None = None
    pm_alert_read_sync_store: PmAlertReadSyncStore | None = None
    pm_alert_deferred_store: PmAlertDeferredStore | None = None
    pm_alerts_auto_delete_task: asyncio.Task[Any] | None = None
    pm_alerts_read_sync_task: asyncio.Task[Any] | None = None
    pm_alerts_deferred_task: asyncio.Task[Any] | None = None
    email_pm_alerts_batch_store: EmailPmBatchStore | None = None
    email_pm_alerts_batch_task: asyncio.Task[Any] | None = None
    pm_alert_excluded_chat_ids: set[int] = set()
    need_bot_client = settings.pm_alerts_enabled or settings.forwarding_enabled
    if need_bot_client:
        # Keep bot auth stateless to prevent accidental reuse of a user-authorized sqlite session.
        bot_client = TelegramClient(MemorySession(), settings.api_id, settings.api_hash)
        await bot_client.start(bot_token=settings.bot_token)
        bot_identity = await bot_client.get_me()
        if not bool(getattr(bot_identity, "bot", False)):
            raise RuntimeError(
                "BOT_TOKEN authentication did not produce a bot account. "
                "Please check BOT_TOKEN and restart."
            )
    if settings.forwarding_enabled:
        bot_target_entity = await bot_client.get_entity(settings.bot_target_chat)
    if settings.forwarding_enabled:
        active_message_map_file = settings.message_map_file_bot
        message_map_store = MessageMapStore(
            active_message_map_file,
            ttl_days=settings.message_map_ttl_days,
        )
    if pm_alerts_active:
        if settings.pm_alerts_enabled:
            pm_alert_target_entity = await bot_client.get_entity(settings.pm_alert_target_chat)
            pm_alert_target_entity_user = await client.get_entity(settings.pm_alert_target_chat)
            pm_alert_target_peer_id = get_peer_id(pm_alert_target_entity_user)
            pm_alerts_store = PmAlertCooldownStore(settings.pm_alerts_file)
            if settings.pm_alert_sync_target_read_state_enabled:
                pm_alert_read_sync_store = PmAlertReadSyncStore(settings.pm_alert_sync_target_read_state_file)
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
        if (
            settings.pm_alerts_enabled
            and settings.pm_alert_deferred_unread_enabled
            and pm_alerts_store is not None
        ):
            pm_alert_deferred_store = PmAlertDeferredStore(settings.pm_alert_deferred_unread_file)
            pm_alerts_deferred_task = asyncio.create_task(
                _pm_alerts_deferred_unread_loop(
                    client=client,
                    bot_client=bot_client,
                    settings=settings,
                    pm_alert_target_entity=pm_alert_target_entity,
                    pm_alert_target_peer_id=pm_alert_target_peer_id,
                    pm_alerts_store=pm_alerts_store,
                    pm_alert_messages_store=pm_alert_messages_store,
                    pm_alert_read_sync_store=pm_alert_read_sync_store,
                    pm_alert_my_activity_store=pm_alert_my_activity_store,
                    deferred_store=pm_alert_deferred_store,
                )
            )
        if (
            settings.pm_alerts_enabled
            and settings.pm_alert_sync_target_read_state_enabled
            and pm_alert_read_sync_store is not None
            and pm_alert_target_entity_user is not None
            and pm_alert_target_peer_id is not None
        ):
            pm_alerts_read_sync_task = asyncio.create_task(
                _pm_alerts_sync_target_read_state_loop(
                    client=client,
                    pm_alert_target_entity_user=pm_alert_target_entity_user,
                    pm_alert_target_peer_id=pm_alert_target_peer_id,
                    read_sync_store=pm_alert_read_sync_store,
                    check_seconds=settings.pm_alert_sync_target_read_state_check_seconds,
                )
            )
    if settings.email_pm_alerts_batch_enabled and email_sender is not None:
        email_pm_alerts_batch_store = EmailPmBatchStore(settings.email_pm_alerts_batch_file)
        email_pm_alerts_batch_task = asyncio.create_task(
            _email_pm_alerts_batch_loop(
                email_sender=email_sender,
                batch_store=email_pm_alerts_batch_store,
            )
        )

    source_peer_ids: set[int] = set()
    target_peer_id: int | None = None
    if source_delivery_enabled:
        source_peer_ids = {get_peer_id(entity) for entity in source_entities}
    if settings.forwarding_enabled:
        try:
            target_peer_id = get_peer_id(await client.get_entity(settings.bot_target_chat))
        except Exception:
            logging.warning("Could not resolve bot delivery target in user account. Target loop protection may be limited.")

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
        bot_me = await bot_client.get_me()
        logging.info("Delivery mode: bot")
        logging.info("Bot sender: %s", bot_me.username or bot_me.id)
        logging.info("Target chat (bot): %s", _entity_label(bot_target_entity))
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
    if settings.email_pm_alerts_batch_enabled:
        logging.info(
            "PM alerts email batch delivery enabled: to=%s, debounce=%s minute(s), file=%s",
            ", ".join(settings.email_to),
            settings.email_pm_alerts_batch_minutes,
            settings.email_pm_alerts_batch_file,
        )
    if pm_alerts_active:
        if settings.pm_alerts_enabled:
            logging.info(
                "PM alerts Telegram delivery enabled: target=%s",
                _entity_label(pm_alert_target_entity),
            )
            logging.info(
                "PM alerts Telegram cooldown: %s minute(s), lang=%s",
                settings.pm_alert_cooldown_minutes,
                settings.pm_alerts_lang,
            )
        else:
            logging.info("PM alerts Telegram delivery disabled (PM_ALERTS_ENABLED=false).")
            logging.info("PM alerts language for templates: %s", settings.pm_alerts_lang)
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
        if settings.pm_alert_deferred_unread_enabled:
            logging.info(
                "PM alerts deferred unread enabled: %s minute(s), file=%s",
                settings.pm_alert_deferred_unread_minutes,
                settings.pm_alert_deferred_unread_file,
            )
            if not settings.pm_alert_require_my_silence:
                logging.warning(
                    "PM_ALERT_DEFERRED_UNREAD_ENABLED=true but PM_ALERT_REQUIRE_MY_SILENCE=false. "
                    "Deferred unread queue will normally stay unused."
                )
        if settings.pm_alert_sync_target_read_state_enabled:
            logging.info(
                "PM alerts target read-state sync enabled: check every %ss, file=%s",
                settings.pm_alert_sync_target_read_state_check_seconds,
                settings.pm_alert_sync_target_read_state_file,
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
                                formatted_text,
                                link_preview=False,
                                parse_mode="html",
                            )
                            sent_target_message_id = _extract_message_id(sent_msg)
                    else:
                        if not original_text:
                            # Skip text-only forwarding to Telegram if there is no text.
                            pass
                        else:
                            sent_msg = await bot_client.send_message(
                                bot_target_entity,
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
        if not pm_alerts_active:
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

        now_ts = int(time.time())
        peer_for_silence = event.chat_id or event.sender_id
        if peer_for_silence is None:
            return
        last_my_message_ts: int | None = None
        silence_blocked = False
        if settings.pm_alert_require_my_silence and pm_alert_my_activity_store is not None:
            min_silence_seconds = settings.pm_alert_min_silence_after_my_message_minutes * 60
            last_my_message_ts = await pm_alert_my_activity_store.get_last_my_message_ts(peer_for_silence)
            if (
                last_my_message_ts is not None
                and (now_ts - last_my_message_ts) < min_silence_seconds
            ):
                silence_blocked = True

        sender = await event.get_sender()
        sender_label = _entity_label(sender)
        alert_text = _build_pm_alert_text(sender_label, settings.pm_alerts_lang)
        telegram_sent = False
        email_queued = False

        if (
            settings.pm_alerts_enabled
            and silence_blocked
            and settings.pm_alert_deferred_unread_enabled
            and pm_alert_deferred_store is not None
        ):
            due_at = now_ts + settings.pm_alert_deferred_unread_minutes * 60
            await pm_alert_deferred_store.upsert(
                sender_id=event.sender_id,
                peer_id=peer_for_silence,
                message_id=message.id,
                sender_label=sender_label,
                due_at=due_at,
            )
            due_dt = datetime.fromtimestamp(due_at).strftime("%Y-%m-%d %H:%M:%S")
            logging.info(
                "Queued deferred unread PM alert for %s (check at %s)",
                sender_label,
                due_dt,
            )

        if silence_blocked:
            return

        if settings.pm_alerts_enabled:
            if pm_alerts_store is None:
                logging.error("PM alerts cooldown store is not initialized")
                return
            should_notify = await _should_send_telegram_pm_alert(
                settings=settings,
                pm_alerts_store=pm_alerts_store,
                sender_id=event.sender_id,
                now_ts=now_ts,
                last_my_message_ts=last_my_message_ts,
            )
            if should_notify:
                telegram_sent = await _send_telegram_pm_alert(
                    bot_client=bot_client,
                    pm_alert_target_entity=pm_alert_target_entity,
                    pm_alert_target_peer_id=pm_alert_target_peer_id,
                    pm_alert_messages_store=pm_alert_messages_store,
                    pm_alert_read_sync_store=pm_alert_read_sync_store,
                    pm_alerts_store=pm_alerts_store,
                    settings=settings,
                    sender_id=event.sender_id,
                    sender_label=sender_label,
                    alert_text=alert_text,
                    source_peer_id=peer_for_silence,
                    source_message_id=message.id,
                )
                if telegram_sent and pm_alert_deferred_store is not None:
                    await pm_alert_deferred_store.remove(event.sender_id)

        if settings.email_pm_alerts_batch_enabled and email_pm_alerts_batch_store is not None:
            try:
                line, attach_media = _format_pm_alert_email_item(message)
                chat_id_for_batch = event.chat_id if event.chat_id is not None else event.sender_id
                if chat_id_for_batch is None:
                    return
                due_at = await email_pm_alerts_batch_store.add_message(
                    sender_id=event.sender_id,
                    sender_label=sender_label,
                    chat_id=chat_id_for_batch,
                    message_id=message.id,
                    line=line,
                    attach_media=attach_media,
                    batch_seconds=settings.email_pm_alerts_batch_minutes * 60,
                )
                email_queued = True
                due_dt = datetime.fromtimestamp(due_at).strftime("%Y-%m-%d %H:%M:%S")
                logging.info("Queued PM alert email batch for %s (flush at %s)", sender_label, due_dt)
            except Exception as exc:
                logging.exception("Failed to queue PM alert email batch for %s: %s", sender_label, exc)

        if telegram_sent or email_queued:
            logging.info(
                "Processed PM alert for %s%s%s",
                sender_label,
                " [telegram]" if telegram_sent else "",
                " [email-batch-queued]" if email_queued else "",
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
                await bot_client.edit_message(
                    bot_target_entity,
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
        if pm_alerts_read_sync_task is not None:
            pm_alerts_read_sync_task.cancel()
            with suppress(asyncio.CancelledError):
                await pm_alerts_read_sync_task
        if pm_alerts_deferred_task is not None:
            pm_alerts_deferred_task.cancel()
            with suppress(asyncio.CancelledError):
                await pm_alerts_deferred_task
        if email_pm_alerts_batch_task is not None:
            email_pm_alerts_batch_task.cancel()
            with suppress(asyncio.CancelledError):
                await email_pm_alerts_batch_task
