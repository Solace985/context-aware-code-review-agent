"""Domain types.

Enum values are persisted as strings, so adding one means every existing row
predates it and every branch over the enum is potentially incomplete.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ReportStatus(str, Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    ARCHIVED = "archived"


TERMINAL_STATUSES = {ReportStatus.APPROVED, ReportStatus.ARCHIVED}


@dataclass
class Report:
    id: int
    owner_id: int
    title: str
    body: str
    status: ReportStatus = ReportStatus.DRAFT

    def is_editable(self) -> bool:
        return self.status not in TERMINAL_STATUSES


@dataclass
class User:
    id: int
    email: str
    role: str = "member"
    is_active: bool = True
    display_name: str = ""
