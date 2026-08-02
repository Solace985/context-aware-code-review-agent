"""User endpoints.

Every handler here follows the same three steps: authenticate, authorise
against the specific row, then touch the database through `db` helpers with
bound parameters.
"""

from __future__ import annotations

import logging
from typing import Any

from .auth import Forbidden, assert_owner, require_user
from .db import clamp_page_size, execute, query, query_one

log = logging.getLogger(__name__)

EDITABLE_PROFILE_FIELDS = {"display_name", "timezone", "locale"}


def get_user(request: dict[str, Any], user_id: int) -> dict[str, Any]:
    caller = require_user(request)
    row = query_one("SELECT id, email, role, display_name FROM users WHERE id = ?", (user_id,))
    if row is None:
        return {"status": 404, "body": {"error": "not found"}}
    assert_owner(caller, {"owner_id": row["id"]})
    return {"status": 200, "body": row}


def list_users(request: dict[str, Any], page_size: int | None = None) -> dict[str, Any]:
    caller = require_user(request)
    if caller.get("role") != "admin":
        raise Forbidden("admin only")
    limit = clamp_page_size(page_size)
    rows = query("SELECT id, email, display_name FROM users ORDER BY id LIMIT ?", (limit,))
    return {"status": 200, "body": {"users": rows, "limit": limit}}


def update_display_name(request: dict[str, Any], user_id: int, display_name: str) -> dict[str, Any]:
    caller = require_user(request)
    row = query_one("SELECT id FROM users WHERE id = ?", (user_id,))
    if row is None:
        return {"status": 404, "body": {"error": "not found"}}
    assert_owner(caller, {"owner_id": row["id"]})

    display_name = (display_name or "").strip()
    if not display_name or len(display_name) > 80:
        return {"status": 400, "body": {"error": "display_name must be 1-80 characters"}}

    execute("UPDATE users SET display_name = ? WHERE id = ?", (display_name, user_id))
    log.info("display name updated for user_id=%s", user_id)
    return {"status": 200, "body": {"id": user_id, "display_name": display_name}}


def deactivate_user(request: dict[str, Any], user_id: int) -> dict[str, Any]:
    caller = require_user(request)
    row = query_one("SELECT id FROM users WHERE id = ?", (user_id,))
    if row is None:
        return {"status": 404, "body": {"error": "not found"}}
    assert_owner(caller, {"owner_id": row["id"]})
    execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
    return {"status": 204, "body": None}
