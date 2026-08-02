"""Shared data types.

Everything that crosses a module boundary is defined here so the rest of the
package can stay flat and import-cycle free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Ordered worst-first. Index doubles as the sort key.
SEVERITIES = ("critical", "high", "medium", "low")

CATEGORIES = (
    "security",
    "correctness",
    "reliability",
    "performance",
    "maintainability",
    "requirements_gap",
    "testing",
)

# Which triage bucket a severity lands in. Mirrors the "action required" vs
# "review recommended" split that the course argues for: the reviewer's job is
# to route human attention, not to make every comment feel equally urgent.
TRIAGE = {
    "critical": "action_required",
    "high": "action_required",
    "medium": "review_recommended",
    "low": "nitpick",
}


def severity_rank(sev: str) -> int:
    try:
        return SEVERITIES.index(sev)
    except ValueError:
        return len(SEVERITIES)


@dataclass
class Hunk:
    """One `@@ ... @@` block of a unified diff."""

    new_start: int
    new_count: int
    added: list[tuple[int, str]] = field(default_factory=list)  # (new line no, text)
    removed: list[str] = field(default_factory=list)
    # Ordered body for rendering: (kind, new line no or 0, text); kind is "+", "-" or " ".
    lines: list[tuple[str, int, str]] = field(default_factory=list)


@dataclass
class ChangedFile:
    path: str
    old_path: str | None = None
    status: str = "modified"  # added | modified | deleted | renamed
    binary: bool = False
    hunks: list[Hunk] = field(default_factory=list)
    raw: str = ""

    @property
    def added_line_numbers(self) -> set[int]:
        return {ln for h in self.hunks for ln, _ in h.added}


@dataclass
class Diff:
    files: list[ChangedFile] = field(default_factory=list)
    raw: str = ""
    truncated: bool = False
    dropped_files: list[str] = field(default_factory=list)
    truncated_hunks: int = 0

    @property
    def paths(self) -> list[str]:
        return [f.path for f in self.files]

    def by_path(self, path: str) -> ChangedFile | None:
        for f in self.files:
            if f.path == path:
                return f
        return None


@dataclass
class Chunk:
    """A retrievable unit of repository code."""

    path: str
    kind: str  # function | class | module | window
    name: str
    start_line: int
    end_line: int
    text: str

    @property
    def label(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line} ({self.kind} {self.name})"


@dataclass
class Rule:
    id: str
    title: str
    path: str
    severity: str
    text: str


@dataclass
class ReviewContext:
    """Everything (besides the diff) that the agents are allowed to see."""

    chunks: list[Chunk] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    task: str = ""
    task_source: str = ""
    mode: str = "selective"
    indexed_files: int = 0
    indexed_chunks: int = 0
    index_capped: bool = False
    redactions: int = 0
    skipped_sensitive: list[str] = field(default_factory=list)


@dataclass
class Finding:
    title: str
    category: str
    severity: str
    confidence: float
    file: str
    start_line: int
    end_line: int
    description: str
    evidence: str
    suggestion: str
    rule_ids: list[str] = field(default_factory=list)
    found_by: list[str] = field(default_factory=list)

    @property
    def triage(self) -> str:
        return TRIAGE.get(self.severity, "nitpick")

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "category": self.category,
            "severity": self.severity,
            "triage": self.triage,
            "confidence": round(self.confidence, 2),
            "file": self.file,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "description": self.description,
            "evidence": self.evidence,
            "suggestion": self.suggestion,
            "rule_ids": self.rule_ids,
            "found_by": self.found_by,
        }


@dataclass
class AgentRun:
    name: str
    ok: bool
    raw_findings: int = 0
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class ReviewResult:
    findings: list[Finding] = field(default_factory=list)
    diff: Diff = field(default_factory=Diff)
    context: ReviewContext = field(default_factory=ReviewContext)
    agents: list[AgentRun] = field(default_factory=list)
    model: str = ""
    elapsed_s: float = 0.0
    dropped: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def input_tokens(self) -> int:
        return sum(a.input_tokens for a in self.agents)

    @property
    def output_tokens(self) -> int:
        return sum(a.output_tokens for a in self.agents)

    def by_triage(self, bucket: str) -> list[Finding]:
        return [f for f in self.findings if f.triage == bucket]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "elapsed_s": round(self.elapsed_s, 2),
            "findings": [f.to_dict() for f in self.findings],
            "counts": {
                s: sum(1 for f in self.findings if f.severity == s) for s in SEVERITIES
            },
            "context_used": {
                "mode": self.context.mode,
                "indexed_files": self.context.indexed_files,
                "indexed_chunks": self.context.indexed_chunks,
                "retrieved_chunks": [c.label for c in self.context.chunks],
                "rules": [{"id": r.id, "title": r.title, "path": r.path} for r in self.context.rules],
                "task_source": self.context.task_source,
                "redactions": self.context.redactions,
                "skipped_sensitive": self.context.skipped_sensitive,
            },
            "changed_files": [
                {"path": f.path, "status": f.status, "added_lines": len(f.added_line_numbers)}
                for f in self.diff.files
            ],
            "agents": [
                {
                    "name": a.name,
                    "ok": a.ok,
                    "raw_findings": a.raw_findings,
                    "error": a.error,
                    "input_tokens": a.input_tokens,
                    "output_tokens": a.output_tokens,
                }
                for a in self.agents
            ],
            "dropped": self.dropped,
            "warnings": self.warnings,
        }


class ReviewError(Exception):
    """Any user-facing failure. The CLI turns this into exit code 2."""
