"""Envío de correo por SMTP (opcional). Si no está configurado, las funciones devuelven False sin error."""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from .config import settings

log = logging.getLogger("portal.mail")


def mail_enabled() -> bool:
    return bool(settings.smtp_host and settings.smtp_from)


def send_mail(to: str, subject: str, body: str, attachments: list[tuple[str, bytes, str]] | None = None) -> bool:
    if not mail_enabled() or not to:
        return False
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = settings.smtp_from, to, subject
    msg.set_content(body)
    for filename, data, mime in attachments or []:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    try:
        ctx = ssl.create_default_context()
        if settings.smtp_port == 465:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=ctx, timeout=20)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
            if settings.smtp_starttls:
                server.starttls(context=ctx)
        with server:
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError) as exc:
        log.warning("No se pudo enviar el correo a %s: %s", to, exc)
        return False
