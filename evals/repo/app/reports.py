"""Report endpoints."""

from __future__ import annotations

import logging
from typing import Any

from .auth import assert_owner, require_user
from .db import clamp_page_size, execute, query, query_one
from .models import TERMINAL_STATUSES, ReportStatus

log = logging.getLogger(__name__)


def get_report(request: dict[str, Any], report_id: int) -> dict[str, Any]:
    caller = require_user(request)
    row = query_one("SELECT * FROM reports WHERE id = ?", (report_id,))
    if row is None:
        return {"status": 404, "body": {"error": "not found"}}
    assert_owner(caller, row)
    return {"status": 200, "body": row}


def list_reports(request: dict[str, Any], page_size: int | None = None) -> dict[str, Any]:
    caller = require_user(request)
    limit = clamp_page_size(page_size)
    rows = query(
        "SELECT id, title, status FROM reports WHERE owner_id = ? ORDER BY id DESC LIMIT ?",
        (caller["id"], limit),
    )
    return {"status": 200, "body": {"reports": rows, "limit": limit}}


def search_reports(request: dict[str, Any], term: str, page_size: int | None = None) -> dict[str, Any]:
    caller = require_user(request)
    limit = clamp_page_size(page_size)
    rows = query(
        "SELECT id, title, status FROM reports WHERE owner_id = ? AND title LIKE ? LIMIT ?",
        (caller["id"], f"%{term}%", limit),
    )
    return {"status": 200, "body": {"reports": rows, "limit": limit}}


def submit_report(request: dict[str, Any], report_id: int) -> dict[str, Any]:
    caller = require_user(request)
    row = query_one("SELECT * FROM reports WHERE id = ?", (report_id,))
    if row is None:
        return {"status": 404, "body": {"error": "not found"}}
    assert_owner(caller, row)

    status = ReportStatus(row.get("status") or ReportStatus.DRAFT.value)
    if status in TERMINAL_STATUSES:
        return {"status": 409, "body": {"error": f"report is {status.value} and cannot be submitted"}}

    execute(
        "UPDATE reports SET status = ? WHERE id = ?",
        (ReportStatus.SUBMITTED.value, report_id),
    )
    log.info("report submitted report_id=%s", report_id)
    return {"status": 200, "body": {"id": report_id, "status": ReportStatus.SUBMITTED.value}}


def approve_report(request: dict[str, Any], report_id: int) -> dict[str, Any]:
    caller = require_user(request)
    row = query_one("SELECT * FROM reports WHERE id = ?", (report_id,))
    if row is None:
        return {"status": 404, "body": {"error": "not found"}}
    assert_owner(caller, row)

    status = ReportStatus(row.get("status") or ReportStatus.DRAFT.value)
    if status in TERMINAL_STATUSES:
        return {"status": 409, "body": {"error": "already finalised"}}

    execute("UPDATE reports SET status = ? WHERE id = ?", (ReportStatus.APPROVED.value, report_id))
    return {"status": 200, "body": {"id": report_id, "status": ReportStatus.APPROVED.value}}
