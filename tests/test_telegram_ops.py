import tempfile
import types
import unittest

from autoforwarder.stores import PmAlertCooldownStore
from autoforwarder.telegram_ops import (
    _build_pm_alert_text,
    _format_email_forward_plain,
    _format_pm_alert_email_item,
    _format_prefixed_html,
    _safe_media_filename,
    _should_send_telegram_pm_alert,
)


class TelegramOpsTests(unittest.IsolatedAsyncioTestCase):
    def test_format_prefixed_html_with_quote_and_link(self) -> None:
        rendered = _format_prefixed_html(
            "Work Chat",
            "Body text",
            message_url="https://t.me/c/1/2",
            quote_text="Quoted",
        )
        self.assertIn("<a href=\"https://t.me/c/1/2\">", rendered)
        self.assertIn("<b>[Work Chat]</b>", rendered)
        self.assertIn("<blockquote>Quoted</blockquote>", rendered)
        self.assertTrue(rendered.endswith("Body text"))

    def test_format_email_forward_plain_order(self) -> None:
        body = _format_email_forward_plain(
            "main",
            quote_text="line1\nline2",
            message_url="https://t.me/c/1/2",
        )
        self.assertIn("> line1\n> line2\n\nmain\n\nhttps://t.me/c/1/2", body)

    def test_pm_alert_text_languages(self) -> None:
        self.assertEqual(_build_pm_alert_text("Alice", "eng"), "Alice sent a new message")
        self.assertEqual(_build_pm_alert_text("Alice", "ru"), "Alice отправил(-а) новое сообщение")

    def test_pm_alert_email_item_variants(self) -> None:
        sticker_message = types.SimpleNamespace(
            message="",
            sticker=True,
            voice=False,
            video_note=False,
            photo=None,
            document=None,
            media=object(),
        )
        text, attach_media = _format_pm_alert_email_item(sticker_message)
        self.assertEqual(text, "[sticker]")
        self.assertFalse(attach_media)

        photo_message = types.SimpleNamespace(
            message="",
            sticker=False,
            voice=False,
            video_note=False,
            photo=object(),
            document=None,
            media=object(),
        )
        text, attach_media = _format_pm_alert_email_item(photo_message)
        self.assertEqual(text, "[file]")
        self.assertFalse(attach_media)

    def test_safe_media_filename_uses_mime_extension(self) -> None:
        file_meta = types.SimpleNamespace(name=None, ext=None, mime_type="application/pdf")
        message = types.SimpleNamespace(file=file_meta)
        filename = _safe_media_filename(message)
        self.assertTrue(filename.endswith(".pdf"), filename)

    async def test_should_send_telegram_pm_alert_new_cycle_after_my_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PmAlertCooldownStore(f"{tmp}/cooldown.json")
            await store.touch_alert(sender_id=1, cooldown_seconds=60)
            last_alert_ts = await store.get_last_alert_ts(1)
            self.assertIsNotNone(last_alert_ts)

            settings = types.SimpleNamespace(pm_alert_cooldown_minutes=60)

            blocked = await _should_send_telegram_pm_alert(
                settings=settings,
                pm_alerts_store=store,
                sender_id=1,
                now_ts=int(last_alert_ts) + 10,
                last_my_message_ts=int(last_alert_ts) - 1,
            )
            self.assertFalse(blocked)

            allowed_after_reply = await _should_send_telegram_pm_alert(
                settings=settings,
                pm_alerts_store=store,
                sender_id=1,
                now_ts=int(last_alert_ts) + 10,
                last_my_message_ts=int(last_alert_ts) + 1,
            )
            self.assertTrue(allowed_after_reply)


if __name__ == "__main__":
    unittest.main()
