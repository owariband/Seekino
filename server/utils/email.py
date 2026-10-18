from __future__ import annotations

import smtplib
import ssl
import re
from email.message import EmailMessage

from ..config.email import SMTPConfig


def normalize_email(value: str) -> str:
    if not value.isascii():
        raise ValueError("请输入有效的邮箱地址")
    email = value.strip().lower()
    if len(email) > 254 or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}", email, flags=re.ASCII):
        raise ValueError("请输入有效的邮箱地址")
    local, domain = email.rsplit("@", 1)
    if (len(local) > 64 or local.startswith(".") or local.endswith(".")
            or ".." in email or any(part.startswith("-") or part.endswith("-")
                                   or len(part) > 63 for part in domain.split("."))):
        raise ValueError("请输入有效的邮箱地址")
    return email


class SMTPEmailSender:
    def __init__(self, config: SMTPConfig) -> None:
        self._config = config

    def send_code(self, email: str, code: str, ttl_seconds: int) -> None:
        config = self._config
        if not config.host or not config.sender:
            raise RuntimeError("SMTP is not configured")
        message = EmailMessage()
        message["Subject"] = "Seekino 登录验证码"
        message["From"] = config.sender
        message["To"] = email
        message.set_content(f"你的 Seekino 登录验证码是 {code}，{ttl_seconds} 秒内有效。请勿将验证码告诉他人。")
        client = smtplib.SMTP_SSL if config.use_ssl else smtplib.SMTP
        options = {"timeout": config.timeout_seconds}
        if config.use_ssl:
            options["context"] = ssl.create_default_context()
        with client(config.host, config.port, **options) as smtp:
            if config.start_tls:
                smtp.starttls(context=ssl.create_default_context())
            if config.username:
                smtp.login(config.username, config.password)
            smtp.send_message(message)
