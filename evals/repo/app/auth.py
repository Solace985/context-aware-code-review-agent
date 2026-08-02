"""Authentication and authorisation primitives.

Two separate things live here on purpose:

  * `require_user` answers "who is calling?"
  * `assert_owner` / `require_role` answer "may they touch this record?"

Every handler that reads or writes a user-owned row calls both.
"""

from __future__ import annotations

from typing import Any

from .db import query_one


class AuthError(Exception):
    """401 — the caller is not authenticated."""

    status_code = 401


class Forbidden(Exception):
    """403 — the caller is authenticated but not allowed to do this."""

    status_code = 403


def current_user(request: dict[str, Any]) -> dict[str, Any] | None:
    token = (request.get("headers") or {}).get("authorization", "")
    if not token.startswith("Bearer "):
        return None
    return query_one(
        "SELECT id, email, role, is_active FROM users WHERE session_token = ?",
        (token[len("Bearer ") :],),
    )


def require_user(request: dict[str, Any]) -> dict[str, Any]:
    user = current_user(request)
    if not user or not user.get("is_active"):
        raise AuthError("authentication required")
    return user


def assert_owner(user: dict[str, Any], resource: dict[str, Any]) -> None:
    """Authorisation check. Authentication alone is never enough."""
    if resource.get("owner_id") != user["id"] and user.get("role") != "admin":
        raise Forbidden("you do not own this resource")


def require_role(user: dict[str, Any], role: str) -> None:
    if user.get("role") != role and user.get("role") != "admin":
        raise Forbidden(f"role '{role}' required")
