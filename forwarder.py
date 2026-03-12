import asyncio
import argparse
from getpass import getpass
import json
import logging
import os
import sys
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
    auth_mode: str
    skip_outgoing: bool
    allowed_senders: list[str]
    chat_allowed_senders: dict[str, list[str]]


def _parse_bool(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_auth_mode(value: str | None) -> str:
    mode = (value or "phone").strip().lower()
    if mode not in {"phone", "qr"}:
        raise ValueError("AUTH_MODE must be either 'phone' or 'qr'")
    return mode


def _parse_refs_csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


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

    if not api_id_raw:
        raise ValueError("Environment variable API_ID is required")
    if not api_hash:
        raise ValueError("Environment variable API_HASH is required")

    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise ValueError("API_ID must be an integer") from exc

    source_chats: list[str] = []
    target_chat_ref: str | int | None = None
    if require_routing:
        if not source_chats_raw.strip():
            raise ValueError("Environment variable SOURCE_CHATS is required")
        if not target_chat.strip():
            raise ValueError("Environment variable TARGET_CHAT is required")

        source_chats = [item.strip() for item in source_chats_raw.split(",") if item.strip()]
        if not source_chats:
            raise ValueError("SOURCE_CHATS must contain at least one chat reference")

        target_chat_ref = _coerce_ref(target_chat.strip())

    return Settings(
        api_id=api_id,
        api_hash=api_hash,
        session_name=os.getenv("SESSION_NAME", "autoforwarder"),
        source_chats=source_chats,
        target_chat=target_chat_ref,
        auth_mode=_parse_auth_mode(os.getenv("AUTH_MODE")),
        skip_outgoing=_parse_bool(os.getenv("SKIP_OUTGOING"), default=True),
        allowed_senders=_parse_refs_csv(os.getenv("ALLOWED_SENDERS")),
        chat_allowed_senders=_parse_chat_allowed_senders(os.getenv("CHAT_ALLOWED_SENDERS")),
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

    source_peer_ids = {get_peer_id(entity) for entity in source_entities}
    target_peer_id = get_peer_id(target_entity)
    global_allowed_sender_ids = await _resolve_allowed_sender_ids(client, settings.allowed_senders)
    chat_allowed_sender_ids = await _resolve_chat_sender_filters(client, settings.chat_allowed_senders)

    if target_peer_id in source_peer_ids:
        logging.warning("Target chat is also in SOURCE_CHATS. Messages from it will be ignored to avoid loops.")
    for chat_peer_id in chat_allowed_sender_ids:
        if chat_peer_id not in source_peer_ids:
            logging.warning(
                "CHAT_ALLOWED_SENDERS contains chat %s that is not in SOURCE_CHATS. This filter will not be used.",
                chat_peer_id,
            )

    me = await client.get_me()
    logging.info("Connected as %s", me.username or me.id)
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

        if event.chat_id == target_peer_id:
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
        prefix = f"[{source_title}]"
        original_text = (message.message or "").strip()

        try:
            if message.media:
                caption = f"{prefix} {original_text}".strip()
                try:
                    await client.send_file(
                        target_entity,
                        file=message.media,
                        caption=caption,
                        link_preview=False,
                    )
                except Exception:
                    # Some media types may not allow captions.
                    await client.send_message(target_entity, prefix, link_preview=False)
                    await client.forward_messages(target_entity, message)
            else:
                if not original_text:
                    return
                await client.send_message(
                    target_entity,
                    f"{prefix} {original_text}",
                    link_preview=False,
                )

            await client(functions.messages.MarkDialogUnreadRequest(peer=target_entity, unread=True))
            logging.info("Forwarded message from %s", source_title)
        except Exception as exc:
            logging.exception("Failed to forward message from %s: %s", source_title, exc)

    logging.info("Forwarder is running. Press Ctrl+C to stop.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
