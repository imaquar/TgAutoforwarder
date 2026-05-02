import tempfile
import types
import unittest
from unittest import mock

from autoforwarder.stores import PmAlertCooldownStore
from autoforwarder.telegram_ops import (
    _build_pm_alert_text,
    _format_email_forward_plain,
    _format_pm_alert_email_item,
    _format_prefixed_html,
    _message_text_as_html,
    _safe_media_filename,
    _send_album_as_bot,
    _should_send_as_document_for_quality,
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

    def test_format_prefixed_html_keeps_html_links(self) -> None:
        rendered = _format_prefixed_html(
            "Work Chat",
            'Check <a href="https://example.com">example</a>',
            text_is_html=True,
        )
        self.assertIn('<a href="https://example.com">example</a>', rendered)

    def test_format_email_forward_plain_order(self) -> None:
        body = _format_email_forward_plain(
            "main",
            quote_text="line1\nline2",
            message_url="https://t.me/c/1/2",
        )
        self.assertIn("> line1\n> line2\n\nmain\n\nhttps://t.me/c/1/2", body)

    def test_message_text_as_html_prefers_text_html(self) -> None:
        message = types.SimpleNamespace(
            text_html='Visit <a href="https://example.com">link</a>',
            message="Visit link",
        )
        self.assertEqual(
            _message_text_as_html(message),  # type: ignore[arg-type]
            'Visit <a href="https://example.com">link</a>',
        )

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

    def test_should_send_as_document_for_quality_video(self) -> None:
        video_message = types.SimpleNamespace(video=True, video_note=False, document=None)
        self.assertTrue(_should_send_as_document_for_quality(video_message))  # type: ignore[arg-type]

        video_doc_message = types.SimpleNamespace(
            video=False,
            video_note=False,
            document=types.SimpleNamespace(mime_type="video/mp4"),
        )
        self.assertTrue(_should_send_as_document_for_quality(video_doc_message))  # type: ignore[arg-type]

        photo_message = types.SimpleNamespace(video=False, video_note=False, document=None, photo=object())
        self.assertFalse(_should_send_as_document_for_quality(photo_message))  # type: ignore[arg-type]

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

    async def test_send_album_as_bot_retries_without_caption_list(self) -> None:
        message = types.SimpleNamespace(
            file=types.SimpleNamespace(name="doc.txt", ext=".txt", mime_type="text/plain")
        )
        messages = [message, message, message]
        downloaded_paths = ["/tmp/a.txt", "/tmp/b.txt", "/tmp/c.txt"]

        class _FakeBotClient:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            async def send_file(self, entity, **kwargs):  # type: ignore[no-untyped-def]
                self.calls.append(kwargs)
                if isinstance(kwargs.get("caption"), list):
                    raise RuntimeError("caption list failed")
                return types.SimpleNamespace(id=1)

        bot_client = _FakeBotClient()

        with mock.patch(
            "autoforwarder.telegram_ops._download_media_to_path",
            side_effect=downloaded_paths,
        ):
            result = await _send_album_as_bot(
                source_client=object(),  # type: ignore[arg-type]
                bot_client=bot_client,  # type: ignore[arg-type]
                bot_target_entity=object(),
                messages=messages,  # type: ignore[arg-type]
                captions=["<b>[Chat]</b>", "", ""],
            )

        self.assertEqual(getattr(result, "id", None), 1)
        self.assertEqual(len(bot_client.calls), 2)
        self.assertIsInstance(bot_client.calls[0].get("caption"), list)
        self.assertEqual(bot_client.calls[1].get("caption"), "<b>[Chat]</b>")


if __name__ == "__main__":
    unittest.main()
