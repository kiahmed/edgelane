"""Transactional email for the contact-form route.

Transport is pluggable, resolved per-send from config:
  * SMTP    — if SMTP_HOST is set (e.g. Google Workspace: smtp.gmail.com:587 with
              an App Password, or the smtp-relay.gmail.com relay). Uses stdlib
              smtplib (no extra deps); blocking I/O is run in a worker thread.
  * Resend  — else if RESEND_API_KEY is set (HTTP API via httpx).
  * none    — else logged, not sent.
EMAIL_PROVIDER (auto|smtp|resend) can force one; default 'auto' picks as above.

Sending is best-effort and NEVER raises: callers treat a False return as
"logged, not sent" so an email outage can't fail an otherwise-successful request.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import smtplib
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Optional

import httpx

from .config import get_settings

log = logging.getLogger("edgelane.market.email")

_RESEND_URL = "https://api.resend.com/emails"


def _provider(settings) -> str:
    choice = (settings.email_provider or "auto").strip().lower()
    if choice in ("smtp", "resend"):
        return choice
    if settings.smtp_host:
        return "smtp"
    if settings.resend_api_key:
        return "resend"
    return "none"


async def send_email(
    to_email: str,
    subject: str,
    html: str,
    *,
    reply_to: Optional[str] = None,
    attachment: Optional[tuple[str, bytes]] = None,
) -> bool:
    """Send one email. attachment = (filename, raw_bytes) or None.
    Returns True on success, False on any failure / missing config (logged)."""
    settings = get_settings()
    if not to_email:
        log.warning("[email] no recipient (SUPPORT_EMAIL unset) — skipping: %s", subject)
        return False

    provider = _provider(settings)
    if provider == "smtp":
        return await asyncio.to_thread(
            _send_smtp, settings, to_email, subject, html, reply_to, attachment)
    if provider == "resend":
        return await _send_resend(settings, to_email, subject, html, reply_to, attachment)
    log.warning("[email] no transport configured (set SMTP_HOST or RESEND_API_KEY) — "
                "would email %s: %s", to_email, subject)
    return False


def _send_smtp(settings, to_email, subject, html, reply_to, attachment) -> bool:
    """Blocking SMTP send via stdlib smtplib (run in a thread)."""
    msg = EmailMessage()
    msg["From"] = settings.contact_from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content("This message requires an HTML-capable client.")
    msg.add_alternative(html, subtype="html")
    if attachment:
        filename, content = attachment
        msg.add_attachment(content, maintype="application", subtype="octet-stream",
                           filename=filename)
    # envelope sender = the bare address out of "Name <addr>"
    envelope_from = parseaddr(settings.contact_from_email)[1] or settings.smtp_user
    try:
        if settings.smtp_use_ssl:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
        with server:
            server.ehlo()
            if settings.smtp_starttls and not settings.smtp_use_ssl:
                server.starttls()
                server.ehlo()
            if settings.smtp_user and settings.smtp_password:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg, from_addr=envelope_from, to_addrs=[to_email])
        return True
    except Exception as exc:
        log.error("[email] SMTP send error for %s via %s:%s: %s",
                  to_email, settings.smtp_host, settings.smtp_port, exc)
        return False


async def _send_resend(settings, to_email, subject, html, reply_to, attachment) -> bool:
    payload: dict = {
        "from": settings.contact_from_email,
        "to": [to_email],
        "subject": subject,
        "html": html,
    }
    if reply_to:
        payload["reply_to"] = reply_to
    if attachment:
        filename, content = attachment
        payload["attachments"] = [{
            "filename": filename,
            "content": base64.b64encode(content).decode("ascii"),
        }]
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                _RESEND_URL,
                headers={"Authorization": f"Bearer {settings.resend_api_key}",
                         "Content-Type": "application/json"},
                json=payload,
            )
        if r.status_code >= 300:
            log.error("[email] Resend failed (%s): %s", r.status_code, r.text[:300])
            return False
        return True
    except Exception as exc:
        log.error("[email] Resend send error for %s: %s", to_email, exc)
        return False
