import asyncio
from getpass import getpass
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
    target_chat: str | int
    auth_mode: str
    skip_outgoing: bool


def _parse_bool(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_auth_mode(value: str | None) -> str:
    mode = (value or "phone").strip().lower()
    if mode not in {"phone", "qr"}:
        raise ValueError("AUTH_MODE must be either 'phone' or 'qr'")
    return mode


def load_settings() -> Settings:
    load_dotenv()

    api_id_raw = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    source_chats_raw = os.getenv("SOURCE_CHATS", "")
    target_chat = os.getenv("TARGET_CHAT", "")

    if not api_id_raw:
        raise ValueError("Environment variable API_ID is required")
    if not api_hash:
        raise ValueError("Environment variable API_HASH is required")
    if not source_chats_raw.strip():
        raise ValueError("Environment variable SOURCE_CHATS is required")
    if not target_chat.strip():
        raise ValueError("Environment variable TARGET_CHAT is required")

    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise ValueError("API_ID must be an integer") from exc

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
    settings = load_settings()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    client = TelegramClient(settings.session_name, settings.api_id, settings.api_hash)
    await _authorize_client(client, settings)

    source_entities = await _resolve_entities(client, settings.source_chats)
    target_entity = await client.get_entity(settings.target_chat)

    source_peer_ids = {get_peer_id(entity) for entity in source_entities}
    target_peer_id = get_peer_id(target_entity)

    if target_peer_id in source_peer_ids:
        logging.warning("Target chat is also in SOURCE_CHATS. Messages from it will be ignored to avoid loops.")

    me = await client.get_me()
    logging.info("Connected as %s", me.username or me.id)
    logging.info("Target chat: %s", _entity_label(target_entity))
    logging.info("Source chats: %s", ", ".join(_entity_label(entity) for entity in source_entities))

    @client.on(events.NewMessage(chats=source_entities))
    async def forward_message(event: events.NewMessage.Event) -> None:
        if settings.skip_outgoing and event.out:
            return

        if event.chat_id == target_peer_id:
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
