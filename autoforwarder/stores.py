import asyncio
import json
import logging
import os
import time
from typing import Any



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

    async def get_last_alert_ts(self, sender_id: int) -> int | None:
        async with self._lock:
            last_alert = self._data.get(str(sender_id))
            return int(last_alert) if isinstance(last_alert, int) else None

    async def touch_alert(self, sender_id: int, cooldown_seconds: int) -> None:
        async with self._lock:
            self._data[str(sender_id)] = int(time.time())
            self._prune_old_records_locked(cooldown_seconds)
            self._save()


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

    async def get_last_my_message_ts(self, peer_id: int) -> int | None:
        async with self._lock:
            last_ts = self._data.get(str(peer_id))
            return int(last_ts) if isinstance(last_ts, int) else None


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


class PmAlertReadSyncStore:
    def __init__(self, path: str, keep_days: int = 30) -> None:
        self.path = path
        self.keep_seconds = keep_days * 24 * 60 * 60
        self._lock = asyncio.Lock()
        self._data: dict[str, dict[str, dict[str, int | None]]] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return

        try:
            with open(self.path, "r", encoding="utf-8") as file_obj:
                payload = json.load(file_obj)
            if isinstance(payload, dict):
                normalized: dict[str, dict[str, dict[str, int | None]]] = {}
                for chat_id, bucket in payload.items():
                    if not isinstance(bucket, dict):
                        continue
                    normalized_bucket: dict[str, dict[str, int | None]] = {}
                    for message_id, value in bucket.items():
                        if isinstance(value, int):
                            # Backward compatibility with old format.
                            normalized_bucket[str(message_id)] = {
                                "created_at": int(value),
                                "source_peer_id": None,
                                "source_message_id": None,
                            }
                            continue
                        if not isinstance(value, dict):
                            continue
                        created_at = value.get("created_at")
                        source_peer_id = value.get("source_peer_id")
                        source_message_id = value.get("source_message_id")
                        if not isinstance(created_at, int):
                            continue
                        normalized_bucket[str(message_id)] = {
                            "created_at": int(created_at),
                            "source_peer_id": int(source_peer_id) if isinstance(source_peer_id, int) else None,
                            "source_message_id": int(source_message_id) if isinstance(source_message_id, int) else None,
                        }
                    if normalized_bucket:
                        normalized[str(chat_id)] = normalized_bucket
                self._data = normalized
                self._prune_old_records_locked()
                self._save()
        except Exception as exc:
            logging.warning("Failed to load PM alerts read-sync file %s: %s", self.path, exc)
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
            old_message_ids = [
                message_id
                for message_id, metadata in bucket.items()
                if int(metadata.get("created_at", 0) or 0) < cutoff
            ]
            for message_id in old_message_ids:
                bucket.pop(message_id, None)
            if not bucket:
                empty_chat_ids.append(chat_id)
        for chat_id in empty_chat_ids:
            self._data.pop(chat_id, None)

    async def add(
        self,
        chat_id: int,
        message_id: int,
        *,
        source_peer_id: int | None = None,
        source_message_id: int | None = None,
    ) -> None:
        async with self._lock:
            chat_key = str(chat_id)
            bucket = self._data.setdefault(chat_key, {})
            bucket[str(message_id)] = {
                "created_at": int(time.time()),
                "source_peer_id": source_peer_id,
                "source_message_id": source_message_id,
            }
            self._prune_old_records_locked()
            self._save()

    async def list_entries(self, chat_id: int) -> list[tuple[int, int | None, int | None]]:
        async with self._lock:
            bucket = self._data.get(str(chat_id), {})
            entries: list[tuple[int, int | None, int | None]] = []
            for message_id, metadata in bucket.items():
                if not str(message_id).isdigit():
                    continue
                if not isinstance(metadata, dict):
                    continue
                entries.append(
                    (
                        int(message_id),
                        int(metadata["source_peer_id"]) if isinstance(metadata.get("source_peer_id"), int) else None,
                        int(metadata["source_message_id"]) if isinstance(metadata.get("source_message_id"), int) else None,
                    )
                )
            entries.sort(key=lambda item: item[0])
            return entries

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

    async def count(self, chat_id: int) -> int:
        async with self._lock:
            bucket = self._data.get(str(chat_id), {})
            return len(bucket)


class PmAlertDeferredStore:
    def __init__(self, path: str, keep_days: int = 7) -> None:
        self.path = path
        self.keep_seconds = keep_days * 24 * 60 * 60
        self._lock = asyncio.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return

        try:
            with open(self.path, "r", encoding="utf-8") as file_obj:
                payload = json.load(file_obj)
            if isinstance(payload, dict):
                normalized: dict[str, dict[str, Any]] = {}
                for sender_id, value in payload.items():
                    if not isinstance(value, dict):
                        continue
                    peer_id = value.get("peer_id")
                    message_id = value.get("message_id")
                    sender_label = value.get("sender_label")
                    due_at = value.get("due_at")
                    updated_at = value.get("updated_at")
                    if (
                        isinstance(peer_id, int)
                        and isinstance(message_id, int)
                        and isinstance(sender_label, str)
                        and isinstance(due_at, int)
                        and isinstance(updated_at, int)
                    ):
                        normalized[str(sender_id)] = {
                            "peer_id": peer_id,
                            "message_id": message_id,
                            "sender_label": sender_label.strip() or str(sender_id),
                            "due_at": due_at,
                            "updated_at": updated_at,
                        }
                self._data = normalized
                self._prune_old_records_locked()
                self._save()
        except Exception as exc:
            logging.warning("Failed to load deferred PM alerts file %s: %s", self.path, exc)
            self._data = {}

    def _save(self) -> None:
        temp_path = f"{self.path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as file_obj:
            json.dump(self._data, file_obj)
        os.replace(temp_path, self.path)

    def _prune_old_records_locked(self) -> None:
        cutoff = int(time.time()) - self.keep_seconds
        keys_to_remove = [
            key
            for key, value in self._data.items()
            if int(value.get("updated_at", 0)) < cutoff
        ]
        for key in keys_to_remove:
            self._data.pop(key, None)

    async def upsert(
        self,
        *,
        sender_id: int,
        peer_id: int,
        message_id: int,
        sender_label: str,
        due_at: int,
    ) -> None:
        async with self._lock:
            self._data[str(sender_id)] = {
                "peer_id": peer_id,
                "message_id": message_id,
                "sender_label": sender_label.strip() or str(sender_id),
                "due_at": due_at,
                "updated_at": int(time.time()),
            }
            self._prune_old_records_locked()
            self._save()

    async def remove(self, sender_id: int) -> None:
        async with self._lock:
            self._data.pop(str(sender_id), None)
            self._prune_old_records_locked()
            self._save()

    async def get_due_entries(self, now_ts: int) -> list[tuple[int, int, int, str]]:
        async with self._lock:
            result: list[tuple[int, int, int, str]] = []
            for sender_key, value in self._data.items():
                due_at = value.get("due_at")
                peer_id = value.get("peer_id")
                message_id = value.get("message_id")
                sender_label = value.get("sender_label")
                if (
                    isinstance(due_at, int)
                    and due_at <= now_ts
                    and isinstance(peer_id, int)
                    and isinstance(message_id, int)
                    and isinstance(sender_label, str)
                ):
                    try:
                        sender_id = int(sender_key)
                    except ValueError:
                        continue
                    result.append((sender_id, peer_id, message_id, sender_label))
            return result

    async def next_due_ts(self) -> int | None:
        async with self._lock:
            due_candidates = [
                int(value["due_at"])
                for value in self._data.values()
                if isinstance(value, dict) and isinstance(value.get("due_at"), int)
            ]
            if not due_candidates:
                return None
            return min(due_candidates)


class EmailPmBatchStore:
    def __init__(self, path: str, keep_days: int = 7) -> None:
        self.path = path
        self.keep_seconds = keep_days * 24 * 60 * 60
        self._lock = asyncio.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return

        try:
            with open(self.path, "r", encoding="utf-8") as file_obj:
                payload = json.load(file_obj)
            if isinstance(payload, dict):
                normalized: dict[str, dict[str, Any]] = {}
                for sender_id, value in payload.items():
                    if not isinstance(value, dict):
                        continue
                    sender_label = value.get("sender_label")
                    due_at = value.get("due_at")
                    updated_at = value.get("updated_at")
                    items = value.get("items")
                    if (
                        isinstance(sender_label, str)
                        and isinstance(due_at, int)
                        and isinstance(updated_at, int)
                    ):
                        normalized_items: list[dict[str, Any]] = []
                        if isinstance(items, list):
                            for item in items:
                                if not isinstance(item, dict):
                                    continue
                                chat_id = item.get("chat_id")
                                message_id = item.get("message_id")
                                line = item.get("line")
                                attach_media = item.get("attach_media", False)
                                if (
                                    isinstance(chat_id, int)
                                    and isinstance(message_id, int)
                                    and isinstance(line, str)
                                    and line.strip()
                                ):
                                    normalized_items.append(
                                        {
                                            "chat_id": chat_id,
                                            "message_id": message_id,
                                            "line": line.strip(),
                                            "attach_media": bool(attach_media),
                                        }
                                    )

                        # Backward compatibility for old format with "lines".
                        if not normalized_items:
                            lines = value.get("lines")
                            if isinstance(lines, list):
                                normalized_items = [
                                    {
                                        "chat_id": 0,
                                        "message_id": 0,
                                        "line": str(item).strip(),
                                        "attach_media": False,
                                    }
                                    for item in lines
                                    if str(item).strip()
                                ]

                        if normalized_items:
                            normalized[str(sender_id)] = {
                                "sender_label": sender_label.strip() or str(sender_id),
                                "due_at": due_at,
                                "updated_at": updated_at,
                                "items": normalized_items,
                            }
                self._data = normalized
                self._prune_old_records_locked()
                self._save()
        except Exception as exc:
            logging.warning("Failed to load email PM alerts batch file %s: %s", self.path, exc)
            self._data = {}

    def _save(self) -> None:
        temp_path = f"{self.path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as file_obj:
            json.dump(self._data, file_obj)
        os.replace(temp_path, self.path)

    def _prune_old_records_locked(self) -> None:
        cutoff = int(time.time()) - self.keep_seconds
        keys_to_remove = [
            key
            for key, value in self._data.items()
            if int(value.get("updated_at", 0)) < cutoff
        ]
        for key in keys_to_remove:
            self._data.pop(key, None)

    async def add_message(
        self,
        *,
        sender_id: int,
        sender_label: str,
        chat_id: int,
        message_id: int,
        line: str,
        attach_media: bool,
        batch_seconds: int,
    ) -> int:
        async with self._lock:
            now = int(time.time())
            sender_key = str(sender_id)
            existing = self._data.get(sender_key)
            items: list[dict[str, Any]]
            if existing and isinstance(existing.get("items"), list):
                items = []
                for item in existing["items"]:
                    if not isinstance(item, dict):
                        continue
                    existing_chat_id = item.get("chat_id")
                    existing_message_id = item.get("message_id")
                    existing_line = item.get("line")
                    if (
                        isinstance(existing_chat_id, int)
                        and isinstance(existing_message_id, int)
                        and isinstance(existing_line, str)
                        and existing_line.strip()
                    ):
                        items.append(
                            {
                                "chat_id": existing_chat_id,
                                "message_id": existing_message_id,
                                "line": existing_line.strip(),
                                "attach_media": bool(item.get("attach_media", False)),
                            }
                        )
            else:
                items = []
            if line.strip():
                items.append(
                    {
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "line": line.strip(),
                        "attach_media": attach_media,
                    }
                )
            due_at = now + batch_seconds
            self._data[sender_key] = {
                "sender_label": sender_label.strip() or sender_key,
                "due_at": due_at,
                "updated_at": now,
                "items": items,
            }
            self._prune_old_records_locked()
            self._save()
            return due_at

    async def get_due_entries(self, now_ts: int) -> list[tuple[int, str, list[dict[str, Any]]]]:
        async with self._lock:
            result: list[tuple[int, str, list[dict[str, Any]]]] = []
            for sender_key, value in self._data.items():
                due_at = value.get("due_at")
                sender_label = value.get("sender_label")
                items = value.get("items")
                if (
                    isinstance(due_at, int)
                    and due_at <= now_ts
                    and isinstance(sender_label, str)
                    and isinstance(items, list)
                ):
                    normalized_items: list[dict[str, Any]] = []
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        chat_id = item.get("chat_id")
                        message_id = item.get("message_id")
                        line = item.get("line")
                        if (
                            isinstance(chat_id, int)
                            and isinstance(message_id, int)
                            and isinstance(line, str)
                            and line.strip()
                        ):
                            normalized_items.append(
                                {
                                    "chat_id": chat_id,
                                    "message_id": message_id,
                                    "line": line.strip(),
                                    "attach_media": bool(item.get("attach_media", False)),
                                }
                            )
                    if normalized_items:
                        try:
                            sender_id = int(sender_key)
                        except ValueError:
                            continue
                        result.append((sender_id, sender_label, normalized_items))
            return result

    async def remove(self, sender_id: int) -> None:
        async with self._lock:
            self._data.pop(str(sender_id), None)
            self._prune_old_records_locked()
            self._save()

    async def postpone(self, sender_id: int, seconds: int) -> None:
        async with self._lock:
            key = str(sender_id)
            value = self._data.get(key)
            if not value:
                return
            now = int(time.time())
            value["due_at"] = now + max(1, seconds)
            value["updated_at"] = now
            self._prune_old_records_locked()
            self._save()

    async def next_due_ts(self) -> int | None:
        async with self._lock:
            due_candidates = [
                int(value["due_at"])
                for value in self._data.values()
                if isinstance(value, dict) and isinstance(value.get("due_at"), int)
            ]
            if not due_candidates:
                return None
            return min(due_candidates)
