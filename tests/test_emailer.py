import tempfile
import unittest
from unittest.mock import patch

from autoforwarder.emailer import EmailSender


class _FakeSMTP:
    def __init__(self, host: str, port: int, timeout: int = 30) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.logged_in = None
        self.sent_messages = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def ehlo(self) -> None:
        return None

    def starttls(self) -> None:
        self.started_tls = True

    def login(self, username: str, password: str) -> None:
        self.logged_in = (username, password)

    def send_message(self, message) -> None:
        self.sent_messages.append(message)


class EmailerTests(unittest.TestCase):
    def test_send_sync_builds_message_and_attachment(self) -> None:
        sender = EmailSender(
            smtp_host="smtp.example.com",
            smtp_port=587,
            use_tls=True,
            smtp_username="user@example.com",
            smtp_password="secret",
            from_addr="from@example.com",
            to_addrs=["to@example.com"],
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/sample.txt"
            with open(path, "w", encoding="utf-8") as f:
                f.write("hello")

            fake = _FakeSMTP("smtp.example.com", 587)
            with patch("autoforwarder.emailer.smtplib.SMTP", return_value=fake):
                sender._send_sync(
                    subject="subj",
                    body="body",
                    attachments=[(path, "sample.txt")],
                )

            self.assertEqual(len(fake.sent_messages), 1)
            msg = fake.sent_messages[0]
            self.assertEqual(msg["Subject"], "subj")
            self.assertEqual(msg["From"], "from@example.com")
            self.assertEqual(msg["To"], "to@example.com")
            self.assertTrue(fake.started_tls)
            self.assertEqual(fake.logged_in, ("user@example.com", "secret"))


if __name__ == "__main__":
    unittest.main()
