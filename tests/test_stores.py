import tempfile
import unittest

from autoforwarder.stores import (
    EmailPmBatchStore,
    MessageMapStore,
    PmAlertCooldownStore,
    PmAlertDeferredStore,
)


class StoresTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_map_store_set_and_get(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MessageMapStore(f"{tmp}/map.json", ttl_days=7)
            await store.set(-1001, 10, 900)
            mapped = await store.get(-1001, 10)
            self.assertEqual(mapped, 900)

    async def test_pm_alert_cooldown_store_respects_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PmAlertCooldownStore(f"{tmp}/cooldown.json")
            first = await store.should_notify(42, cooldown_seconds=60)
            second = await store.should_notify(42, cooldown_seconds=60)
            self.assertTrue(first)
            self.assertFalse(second)

    async def test_deferred_store_due_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PmAlertDeferredStore(f"{tmp}/deferred.json")
            await store.upsert(
                sender_id=1,
                peer_id=111,
                message_id=222,
                sender_label="Alice",
                due_at=100,
            )
            due = await store.get_due_entries(now_ts=100)
            self.assertEqual(due, [(1, 111, 222, "Alice")])

    async def test_email_pm_batch_store_accumulates_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = EmailPmBatchStore(f"{tmp}/batch.json")
            await store.add_message(
                sender_id=7,
                sender_label="Bob",
                chat_id=777,
                message_id=1,
                line="hello",
                attach_media=False,
                batch_seconds=1,
            )
            await store.add_message(
                sender_id=7,
                sender_label="Bob",
                chat_id=777,
                message_id=2,
                line="world",
                attach_media=False,
                batch_seconds=1,
            )

            due_entries = await store.get_due_entries(now_ts=10**10)
            self.assertEqual(len(due_entries), 1)
            sender_id, sender_label, items = due_entries[0]
            self.assertEqual(sender_id, 7)
            self.assertEqual(sender_label, "Bob")
            self.assertEqual([item["line"] for item in items], ["hello", "world"])


if __name__ == "__main__":
    unittest.main()
