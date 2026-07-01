"""Per-user tool entitlements.

A user may use a tool ("market", "torque", …) only if its key is in their
`public.profiles.tools_enabled` set (authoritative, read server-side via the
service_role key). Two bypasses, matching the rest of the auth layer:
  * AUTH_ENABLED=false (dev / tests) → no gating.
  * auth == "admin" / "dev"          → server-owner; always allowed.
"""
from __future__ import annotations

from fastapi import HTTPException

from .config import get_settings
from . import supabase_admin


async def ensure_tool(user: dict, tool: str) -> None:
    """Raise 403 unless `user` is entitled to `tool`. No-op for dev mode and the
    server-owner (admin/dev) bypasses."""
    settings = get_settings()
    if not settings.auth_enabled:
        return
    if user.get("auth") in ("admin", "dev"):
        return
    uid = user.get("id")
    if uid and tool in await supabase_admin.get_user_tools(uid):
        return
    raise HTTPException(403, f"Your account is not enabled for the {tool} tool.")
