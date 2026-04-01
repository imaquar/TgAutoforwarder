import os
import tempfile
import unittest
from unittest.mock import patch

from autoforwarder.config import _parse_chat_allowed_senders, load_settings


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env_patcher = patch.dict(os.environ, {}, clear=True)
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)

        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.old_cwd = os.getcwd()
        os.chdir(self.tmpdir.name)
        self.addCleanup(lambda: os.chdir(self.old_cwd))

    @staticmethod
    def _set_base_env() -> None:
        os.environ["API_ID"] = "123456"
        os.environ["API_HASH"] = "hash"
        os.environ["FORWARDING_ENABLED"] = "false"
        os.environ["PM_ALERTS_ENABLED"] = "false"
        os.environ["EMAIL_FORWARDING_ENABLED"] = "false"
        os.environ["EMAIL_PM_ALERTS_BATCH_ENABLED"] = "false"

    def test_parse_chat_allowed_senders_invalid_json(self) -> None:
        with self.assertRaises(ValueError):
            _parse_chat_allowed_senders('{"-100": ["@boss"}')

    def test_load_settings_forwarding_bot_defaults(self) -> None:
        self._set_base_env()
        os.environ["FORWARDING_ENABLED"] = "true"
        os.environ["SOURCE_CHATS"] = "-100111"
        os.environ["TARGET_CHAT"] = "-100222"
        os.environ["BOT_TOKEN"] = "123:token"

        settings = load_settings(require_routing=True)

        self.assertTrue(settings.forwarding_enabled)
        self.assertEqual(settings.bot_target_chat, -100222)
        self.assertEqual(settings.target_chat, -100222)
        self.assertEqual(settings.message_map_file_bot, "autoforwarder_message_map_bot.json")

    def test_load_settings_requires_bot_token_when_forwarding(self) -> None:
        self._set_base_env()
        os.environ["FORWARDING_ENABLED"] = "true"
        os.environ["SOURCE_CHATS"] = "@source"
        os.environ["TARGET_CHAT"] = "@target"

        with self.assertRaisesRegex(ValueError, "BOT_TOKEN"):
            load_settings(require_routing=True)

    def test_load_settings_nothing_to_run(self) -> None:
        self._set_base_env()

        with self.assertRaisesRegex(ValueError, "Nothing to run"):
            load_settings(require_routing=True)


if __name__ == "__main__":
    unittest.main()
