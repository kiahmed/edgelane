"""Transactional email for the contact-form route.

Transport is pluggable, resolved per-send from config:
  * SMTP    — if SMTP_HOST is set. Works with any relay, including Brevo's
              (smtp-relay.brevo.com:587), which needs an SMTP key (xsmtpsib-…)
              — a DIFFERENT credential from the v3 API key below. Uses stdlib
              smtplib (no extra deps); blocking I/O is run in a worker thread.
  * Brevo   — else if BREVO_API_KEY is set: the v3 HTTP API via httpx. Same key
              and sending domain as facades-portal, so both products send as the
              one authenticated Facades sender.
  * none    — else logged, not sent.
EMAIL_PROVIDER (auto|smtp|brevo) can force one; default 'auto' picks as above.

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

_BREVO_URL = "https://api.brevo.com/v3/smtp/email"


def _provider(settings) -> str:
    choice = (settings.email_provider or "auto").strip().lower()
    if choice in ("smtp", "brevo"):
        return choice
    if settings.smtp_host:
        return "smtp"
    if settings.brevo_api_key:
        return "brevo"
    return "none"


def _address(value: str) -> dict:
    """Split "Display Name <addr@host>" into Brevo's {name, email}.
    SMTP takes the combined string; Brevo insists on the structured form."""
    name, addr = parseaddr(value or "")
    out = {"email": addr or (value or "").strip()}
    if name:
        out["name"] = name
    return out


async def send_email(
    to_email: str,
    subject: str,
    html: str,
    *,
    reply_to: Optional[str] = None,
    attachment: Optional[tuple[str, bytes]] = None,
    from_email: Optional[str] = None,
) -> bool:
    """Send one email. attachment = (filename, raw_bytes) or None.
    from_email overrides the default contact sender (e.g. the product-specific
    Simmer alert sender); it must stay on the authenticated sending domain.
    Returns True on success, False on any failure / missing config (logged)."""
    settings = get_settings()
    if not to_email:
        log.warning("[email] no recipient (SUPPORT_EMAIL unset) — skipping: %s", subject)
        return False

    sender = from_email or settings.contact_from_email
    provider = _provider(settings)
    if provider == "smtp":
        return await asyncio.to_thread(
            _send_smtp, settings, to_email, subject, html, reply_to, attachment, sender)
    if provider == "brevo":
        return await _send_brevo(settings, to_email, subject, html, reply_to, attachment, sender)
    log.warning("[email] no transport configured (set SMTP_HOST or BREVO_API_KEY) — "
                "would email %s: %s", to_email, subject)
    return False


def _send_smtp(settings, to_email, subject, html, reply_to, attachment, sender) -> bool:
    """Blocking SMTP send via stdlib smtplib (run in a thread)."""
    msg = EmailMessage()
    msg["From"] = sender
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
    envelope_from = parseaddr(sender)[1] or settings.smtp_user
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


async def _send_brevo(settings, to_email, subject, html, reply_to, attachment, sender) -> bool:
    payload: dict = {
        "sender": _address(sender),
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": html,
    }
    if reply_to:
        payload["replyTo"] = _address(reply_to)
    if attachment:
        filename, content = attachment
        payload["attachment"] = [{
            "name": filename,
            "content": base64.b64encode(content).decode("ascii"),
        }]
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                _BREVO_URL,
                headers={"api-key": settings.brevo_api_key,
                         "content-type": "application/json",
                         "accept": "application/json"},
                json=payload,
            )
        # Brevo answers 201 + {"messageId": …} on success, not 200.
        if r.status_code >= 300:
            log.error("[email] Brevo failed (%s): %s", r.status_code, r.text[:300])
            return False
        return True
    except Exception as exc:
        log.error("[email] Brevo send error for %s: %s", to_email, exc)
        return False
