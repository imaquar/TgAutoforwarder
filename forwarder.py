import asyncio
import argparse
from getpass import getpass
import html
import json
import logging
import mimetypes
import os
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
    source_chats: list[str]
    target_chat: str | int | None
    delivery_mode: str
    bot_token: str | None
    bot_target_chat: str | int | None
    auth_mode: str
    skip_outgoing: bool
    allowed_senders: list[str]
    chat_allowed_senders: dict[str, list[str]]
    message_map_file: str
    message_map_ttl_days: int


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


def _parse_refs_csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _parse_non_negative_int(value: str | None, default: int) -> int:
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value.strip())
    except ValueError as exc:
        raise ValueError("MESSAGE_MAP_TTL_DAYS must be a non-negative integer") from exc
    if parsed < 0:
        raise ValueError("MESSAGE_MAP_TTL_DAYS must be a non-negative integer")
    return parsed


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
    delivery_mode = _parse_delivery_mode(os.getenv("DELIVERY_MODE"))
    bot_token = (os.getenv("BOT_TOKEN") or "").strip() or None
    bot_target_chat_raw = (os.getenv("BOT_TARGET_CHAT") or "").strip()

    if not api_id_raw:
        raise ValueError("Environment variable API_ID is required")
    if not api_hash:
        raise ValueError("Environment variable API_HASH is required")

    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise ValueError("API_ID must be an integer") from exc

    session_name = os.getenv("SESSION_NAME", "autoforwarder")

    source_chats: list[str] = []
    target_chat_ref: str | int | None = None
    bot_target_chat_ref: str | int | None = None
    if require_routing:
        if not source_chats_raw.strip():
            raise ValueError("Environment variable SOURCE_CHATS is required")
        if not target_chat.strip():
            raise ValueError("Environment variable TARGET_CHAT is required")

        source_chats = [item.strip() for item in source_chats_raw.split(",") if item.strip()]
        if not source_chats:
            raise ValueError("SOURCE_CHATS must contain at least one chat reference")

        target_chat_ref = _coerce_ref(target_chat.strip())
        if delivery_mode == "bot":
            if not bot_token:
                raise ValueError("Environment variable BOT_TOKEN is required when DELIVERY_MODE=bot")
            bot_target_chat_ref = _coerce_ref(bot_target_chat_raw) if bot_target_chat_raw else target_chat_ref

    return Settings(
        api_id=api_id,
        api_hash=api_hash,
        session_name=session_name,
        source_chats=source_chats,
        target_chat=target_chat_ref,
        delivery_mode=delivery_mode,
        bot_token=bot_token,
        bot_target_chat=bot_target_chat_ref,
        auth_mode=_parse_auth_mode(os.getenv("AUTH_MODE")),
        skip_outgoing=_parse_bool(os.getenv("SKIP_OUTGOING"), default=True),
        allowed_senders=_parse_refs_csv(os.getenv("ALLOWED_SENDERS")),
        chat_allowed_senders=_parse_chat_allowed_senders(os.getenv("CHAT_ALLOWED_SENDERS")),
        message_map_file=os.getenv("MESSAGE_MAP_FILE", f"{session_name}_message_map.json"),
        message_map_ttl_days=_parse_non_negative_int(os.getenv("MESSAGE_MAP_TTL_DAYS"), default=7),
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


def _format_prefixed_html(source_title: str, text: str, message_url: str | None = None) -> str:
    escaped_prefix = html.escape(f"[{source_title}]")
    prefix_markup = f"<b>{escaped_prefix}</b>"
    if message_url:
        escaped_url = html.escape(message_url, quote=True)
        prefix_markup = f'<a href="{escaped_url}">{prefix_markup}</a>'

    stripped_text = text.strip()
    if stripped_text:
        return f"{prefix_markup}\n\n{html.escape(stripped_text)}"
    return prefix_markup


async def _send_media_as_bot(
    source_client: TelegramClient,
    bot_client: TelegramClient,
    bot_target_entity: Any,
    message: types.Message,
    caption: str,
) -> Any:
    with tempfile.TemporaryDirectory(prefix="tgfwd_") as temp_dir:
        file_hint = os.path.join(temp_dir, _safe_media_filename(message))
        downloaded = await source_client.download_media(message, file=file_hint)
        if not downloaded:
            raise RuntimeError("Failed to download media for bot delivery")

        if isinstance(downloaded, bytes):
            with open(file_hint, "wb") as out_file:
                out_file.write(downloaded)
            downloaded = file_hint

        return await bot_client.send_file(
            bot_target_entity,
            file=downloaded,
            caption=caption,
            link_preview=False,
            parse_mode="html",
        )


def _extract_message_id(result: Any) -> int | None:
    if result is None:
        return None

    if isinstance(result, list):
        for item in result:
            message_id = getattr(item, "id", None)
            if isinstance(message_id, int):
                return message_id
        return None

    message_id = getattr(result, "id", None)
    return message_id if isinstance(message_id, int) else None


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


async def main() -> None:
    args = _parse_args()
    settings = load_settings(require_routing=not args.list_chats)
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

    source_entities = await _resolve_entities(client, settings.source_chats)
    target_entity = await client.get_entity(settings.target_chat)

    bot_client: TelegramClient | None = None
    bot_target_entity: Any | None = None
    message_map_store: MessageMapStore | None = None
    if settings.delivery_mode == "bot":
        bot_client = TelegramClient(f"{settings.session_name}_bot_sender", settings.api_id, settings.api_hash)
        await bot_client.start(bot_token=settings.bot_token)
        bot_target_entity = await bot_client.get_entity(settings.bot_target_chat)
        message_map_store = MessageMapStore(
            settings.message_map_file,
            ttl_days=settings.message_map_ttl_days,
        )

    source_peer_ids = {get_peer_id(entity) for entity in source_entities}
    target_peer_id: int | None = None
    try:
        if settings.delivery_mode == "bot":
            target_peer_id = get_peer_id(await client.get_entity(settings.bot_target_chat))
        else:
            target_peer_id = get_peer_id(target_entity)
    except Exception:
        logging.warning("Could not resolve delivery target in user account. Target loop protection may be limited.")

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

    me = await client.get_me()
    logging.info("Connected as %s", me.username or me.id)
    if settings.delivery_mode == "bot":
        bot_me = await bot_client.get_me()
        logging.info("Delivery mode: bot")
        logging.info("Bot sender: %s", bot_me.username or bot_me.id)
        logging.info("Target chat (bot): %s", _entity_label(bot_target_entity))
    else:
        logging.info("Delivery mode: user")
        logging.info("Target chat: %s", _entity_label(target_entity))
    logging.info("Source chats: %s", ", ".join(_entity_label(entity) for entity in source_entities))
    if global_allowed_sender_ids:
        logging.info("Global sender filter enabled: %s sender(s)", len(global_allowed_sender_ids))
    if chat_allowed_sender_ids:
        logging.info("Per-chat sender filter enabled for %s chat(s)", len(chat_allowed_sender_ids))

    @client.on(events.NewMessage(chats=source_entities))
    async def forward_message(event: events.NewMessage.Event) -> None:
        if settings.skip_outgoing and event.out:
            return

        if target_peer_id is not None and event.chat_id == target_peer_id:
            return

        allowed_for_chat = chat_allowed_sender_ids.get(event.chat_id)
        if allowed_for_chat is not None:
            if event.sender_id is None or event.sender_id not in allowed_for_chat:
                return
        elif global_allowed_sender_ids:
            if event.sender_id is None or event.sender_id not in global_allowed_sender_ids:
                return

        message = event.message
        if message is None or message.action is not None:
            return

        source = await event.get_chat()
        source_title = _entity_label(source)
        original_text = (message.message or "").strip()
        message_url = _build_message_url(source, message.id)
        formatted_text = _format_prefixed_html(source_title, original_text, message_url=message_url)
        formatted_prefix_only = _format_prefixed_html(source_title, "", message_url=message_url)
        sent_target_message_id: int | None = None

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
                    return
                if settings.delivery_mode == "bot":
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
                settings.delivery_mode == "bot"
                and message_map_store is not None
                and sent_target_message_id is not None
                and event.chat_id is not None
            ):
                await message_map_store.set(event.chat_id, message.id, sent_target_message_id)

            if settings.delivery_mode == "user":
                await client(functions.messages.MarkDialogUnreadRequest(peer=target_entity, unread=True))
            logging.info("Forwarded message from %s", source_title)
        except Exception as exc:
            logging.exception("Failed to forward message from %s: %s", source_title, exc)

    @client.on(events.MessageEdited(chats=source_entities))
    async def edit_forwarded_message(event: events.MessageEdited.Event) -> None:
        if settings.delivery_mode != "bot" or message_map_store is None:
            return

        if settings.skip_outgoing and event.out:
            return

        if target_peer_id is not None and event.chat_id == target_peer_id:
            return

        allowed_for_chat = chat_allowed_sender_ids.get(event.chat_id)
        if allowed_for_chat is not None:
            if event.sender_id is None or event.sender_id not in allowed_for_chat:
                return
        elif global_allowed_sender_ids:
            if event.sender_id is None or event.sender_id not in global_allowed_sender_ids:
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
        message_url = _build_message_url(source, message.id)
        formatted_text = _format_prefixed_html(source_title, original_text, message_url=message_url)

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
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
