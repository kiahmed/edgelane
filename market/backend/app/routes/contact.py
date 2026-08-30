"""Contact / support ticket intake.

POST /contact (multipart/form-data) — see docs/contact_tickets.md.

Flow: validate fields → (optional) store attachment in the private
contact-attachments Storage bucket → insert a contact_tickets row → email
support (Brevo/SMTP via app.emailer). The saved ticket is the source of truth:
the email is best-effort, so a transport hiccup never fails an otherwise-good
submission.

Shared by every Facades product — Matrix and Simmer both POST here — so each
submission carries a `product` field. See _PRODUCTS below.

Auth is optional (the form is reachable from the gated teaser too). If a JWT is
present we capture the user id on the row; otherwise it's anonymous. Abuse is
bounded by the global rate-limit middleware already in front of every route.
"""
from __future__ import annotations

import html as _html
import logging
import re
import uuid
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from .. import auth, emailer, supabase_admin
from ..config import get_settings

log = logging.getLogger("edgelane.market.contact")
router = APIRouter()

_MAX_NAME = 120
_MAX_MESSAGE = 5000
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Mirrors the client accept list: image/*,.pdf,.txt,.csv,.log,.json,.zip
_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg",
                ".pdf", ".txt", ".csv", ".log", ".json", ".zip"}

# Which product the ticket came from → display label for the support email.
# Validated here rather than as a DB CHECK constraint, so a fourth product is a
# one-line code change (see supabase/migrations/0012_contact_ticket_product.sql).
# An unrecognised or absent value becomes "unknown" instead of raising: a support
# ticket must never be lost over a metadata field the user didn't fill in.
_PRODUCTS = {"matrix": "Matrix", "simmer": "Simmer", "torque": "Torque"}
_UNKNOWN_PRODUCT = "unknown"


def _normalise_product(raw: str | None) -> tuple[str, str]:
    """(stored_value, display_label) for the submitted product field."""
    key = (raw or "").strip().lower()
    if key in _PRODUCTS:
        return key, _PRODUCTS[key]
    # Absent (or the explicit sentinel) is the expected case for an older client
    # that predates this field — only a genuinely wrong value is worth a warning.
    if key and key != _UNKNOWN_PRODUCT:
        log.warning("[contact] unrecognised product %r — filing as %s", key, _UNKNOWN_PRODUCT)
    return _UNKNOWN_PRODUCT, "unidentified product"


def _safe_name(name: str) -> str:
    """Strip any path and keep a filesystem/URL-safe basename."""
    base = PurePosixPath(name or "").name or "attachment"
    return re.sub(r"[^A-Za-z0-9._-]", "_", base)[:128]


def _ticket_email_html(name, email, message, attachment_name, user_id, product_label) -> str:
    esc = _html.escape
    who = f"{esc(name)} &lt;{esc(email)}&gt;"
    att = f"<p><b>Attachment:</b> {esc(attachment_name)}</p>" if attachment_name else ""
    uid = f"<p style='color:#94a3b8;font-size:12px'>user_id: {esc(user_id)}</p>" if user_id else \
          "<p style='color:#94a3b8;font-size:12px'>anonymous (no login)</p>"
    body = esc(message).replace("\n", "<br>")
    return (
        '<div style="font-family:system-ui,sans-serif;max-width:560px;margin:0 auto">'
        f'<h2 style="margin-bottom:4px">New {esc(product_label)} contact ticket</h2>'
        f'<p><b>From:</b> {who}</p>'
        f'<p><b>Product:</b> {esc(product_label)}</p>'
        f'{att}'
        f'<div style="background:#0f172a;color:#e2e8f0;padding:12px 14px;border-radius:8px;'
        f'white-space:pre-wrap;line-height:1.5">{body}</div>'
        f'{uid}'
        '</div>'
    )


@router.post("/contact")
async def submit_contact(
    name: str = Form(...),
    email: str = Form(...),
    message: str = Form(...),
    product: str = Form(_UNKNOWN_PRODUCT),
    attachment: UploadFile | None = File(None),
    user: dict | None = Depends(auth.get_optional_user),
):
    settings = get_settings()

    # ── validate ────────────────────────────────────────────────────────────
    name = (name or "").strip()
    email = (email or "").strip()
    message = (message or "").strip()
    if not name or len(name) > _MAX_NAME:
        raise HTTPException(422, detail=f"Name is required and must be ≤{_MAX_NAME} characters.")
    if not _EMAIL_RE.match(email) or len(email) > 254:
        raise HTTPException(422, detail="A valid email address is required.")
    if not message or len(message) > _MAX_MESSAGE:
        raise HTTPException(422, detail=f"Message is required and must be ≤{_MAX_MESSAGE} characters.")
    product_key, product_label = _normalise_product(product)

    ticket_id = str(uuid.uuid4())
    # user_id maps to a uuid column. Real Supabase users have a uuid `sub`; the
    # admin-token bypass yields id="admin", so coerce any non-uuid id to None.
    user_id = (user or {}).get("id")
    try:
        if user_id is not None:
            uuid.UUID(str(user_id))
    except (ValueError, AttributeError, TypeError):
        user_id = None

    # ── attachment (optional) ────────────────────────────────────────────────
    att_path = att_name = None
    att_size = None
    att_for_email = None
    if attachment is not None and attachment.filename:
        raw = await attachment.read()
        att_size = len(raw)
        if att_size > settings.contact_attachment_max_bytes:
            mb = settings.contact_attachment_max_bytes // (1024 * 1024)
            raise HTTPException(413, detail=f"Attachment exceeds the {mb} MB limit.")
        att_name = _safe_name(attachment.filename)
        ext = ("." + att_name.rsplit(".", 1)[-1].lower()) if "." in att_name else ""
        if ext not in _ALLOWED_EXT:
            raise HTTPException(415, detail=f"Attachment type '{ext or '?'}' is not allowed.")
        att_path = f"{ticket_id}/{att_name}"
        ok = await supabase_admin.upload_object(
            settings.contact_bucket, att_path, raw,
            attachment.content_type or "application/octet-stream")
        if not ok:
            # Storage failed — don't lose the ticket; record it without the file.
            log.error("[contact] attachment upload failed; saving ticket %s without it", ticket_id)
            att_path = None
        else:
            att_for_email = (att_name, raw)

    # ── persist ───────────────────────────────────────────────────────────────
    saved = await supabase_admin.insert_row("contact_tickets", {
        "id": ticket_id,
        "user_id": user_id,
        "name": name,
        "email": email,
        "message": message,
        "product": product_key,
        "attachment_path": att_path,
        "attachment_name": att_name,
        "attachment_size": att_size,
    })
    if not saved:
        raise HTTPException(502, detail="Could not save your ticket. Please try again shortly.")

    # ── notify support (best-effort; never fails the request) ──────────────────
    sent = await emailer.send_email(
        settings.support_email,
        # Prefix kept verbatim so any existing support-inbox filter on
        # "[EdgeLane contact" keeps matching; the product is appended, not swapped in.
        subject=f"[EdgeLane contact · {product_label}] {name}",
        html=_ticket_email_html(name, email, message, att_name, user_id, product_label),
        reply_to=email,
        attachment=att_for_email,
    )
    if not sent:
        log.warning("[contact] ticket %s (%s) saved but support email not sent",
                    ticket_id, product_key)

    return {"ok": True, "ticket_id": ticket_id}
