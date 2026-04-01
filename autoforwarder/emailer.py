import asyncio
from email.message import EmailMessage
import mimetypes
import smtplib



class EmailSender:
    def __init__(
        self,
        *,
        smtp_host: str,
        smtp_port: int,
        use_tls: bool,
        smtp_username: str | None,
        smtp_password: str | None,
        from_addr: str,
        to_addrs: list[str],
    ) -> None:
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.use_tls = use_tls
        self.smtp_username = smtp_username
        self.smtp_password = smtp_password
        self.from_addr = from_addr
        self.to_addrs = to_addrs

    def _send_sync(
        self,
        *,
        subject: str,
        body: str,
        attachments: list[tuple[str, str]],
    ) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.from_addr
        message["To"] = ", ".join(self.to_addrs)
        message.set_content(body)

        for file_path, attachment_name in attachments:
            mime_type, _ = mimetypes.guess_type(attachment_name)
            if mime_type:
                maintype, subtype = mime_type.split("/", 1)
            else:
                maintype, subtype = "application", "octet-stream"
            with open(file_path, "rb") as file_obj:
                file_data = file_obj.read()
            message.add_attachment(
                file_data,
                maintype=maintype,
                subtype=subtype,
                filename=attachment_name,
            )

        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as smtp:
            smtp.ehlo()
            if self.use_tls:
                smtp.starttls()
                smtp.ehlo()
            if self.smtp_username:
                smtp.login(self.smtp_username, self.smtp_password or "")
            smtp.send_message(message)

    async def send(
        self,
        *,
        subject: str,
        body: str,
        attachments: list[tuple[str, str]] | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._send_sync,
            subject=subject,
            body=body,
            attachments=attachments or [],
        )
