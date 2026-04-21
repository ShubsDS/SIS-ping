"""Gmail SMTP notifier using an App Password.

Create an app password at https://myaccount.google.com/apppasswords
(requires 2FA) and put it in SMTP_APP_PASSWORD.
"""

from __future__ import annotations

import logging
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

import aiosmtplib

from sis_client import ClassAvailability, sis_class_url

log = logging.getLogger(__name__)


@dataclass
class SMTPConfig:
    host: str
    port: int
    username: str
    password: str
    mail_from: str
    mail_to: str

    @classmethod
    def from_env(cls, env: dict) -> "SMTPConfig":
        return cls(
            host=env.get("SMTP_HOST", "smtp.gmail.com"),
            port=int(env.get("SMTP_PORT", "587")),
            username=env["SMTP_USER"],
            password=env["SMTP_APP_PASSWORD"],
            mail_from=env.get("EMAIL_FROM") or env["SMTP_USER"],
            mail_to=env["EMAIL_TO"],
        )


async def send_seat_open_email(
    cfg: SMTPConfig,
    info: ClassAvailability,
    mnemonic_override: str | None = None,
) -> None:
    label = mnemonic_override or f"{info.subject} {info.catalog_nbr}"
    subject = f"[SIS-ping] Seat open in {label} ({info.class_nbr})"
    body = (
        f"A seat just opened in {label} section {info.section} "
        f"({info.title}).\n\n"
        f"Status:                {info.status}\n"
        f"Enrollment available:  {info.enrollment_available}\n"
        f"Enrollment total:      {info.enrollment_total} / {info.class_capacity}\n"
        f"Wait list:             {info.wait_list_total} / {info.wait_list_capacity}\n\n"
        f"Enroll:  {sis_class_url(info.term, info.class_nbr)}\n\n"
        f"-- SIS-ping"
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.mail_from
    msg["To"] = cfg.mail_to
    msg.set_content(body)

    context = ssl.create_default_context()
    await aiosmtplib.send(
        msg,
        hostname=cfg.host,
        port=cfg.port,
        username=cfg.username,
        password=cfg.password,
        start_tls=True,
        tls_context=context,
        timeout=30,
    )
    log.info("Alert email sent to %s", cfg.mail_to)


async def send_startup_email(cfg: SMTPConfig, info: ClassAvailability) -> None:
    """Optional: confirm the watcher started and which class it's watching."""
    subject = f"[SIS-ping] Watching {info.subject} {info.catalog_nbr}-{info.section} ({info.class_nbr})"
    body = (
        f"SIS-ping is now watching:\n\n"
        f"  {info.subject} {info.catalog_nbr}-{info.section}  ({info.title})\n"
        f"  class_nbr = {info.class_nbr}\n"
        f"  term      = {info.term}\n\n"
        f"Current availability:\n"
        f"  status               = {info.status}\n"
        f"  enrollment_available = {info.enrollment_available}\n"
        f"  enrollment_total     = {info.enrollment_total} / {info.class_capacity}\n"
        f"  wait_list            = {info.wait_list_total} / {info.wait_list_capacity}\n\n"
        f"You'll get an email the moment enrollment_available goes above 0.\n"
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.mail_from
    msg["To"] = cfg.mail_to
    msg.set_content(body)

    await aiosmtplib.send(
        msg,
        hostname=cfg.host,
        port=cfg.port,
        username=cfg.username,
        password=cfg.password,
        start_tls=True,
        timeout=30,
    )
    log.info("Startup email sent to %s", cfg.mail_to)
