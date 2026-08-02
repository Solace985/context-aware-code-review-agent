"""Precision / recall / F1 harness.

This exists so the design claims are checkable rather than asserted. It runs
the same synthetic pull requests through four configurations:

    diff-only     no repository context, one general reviewer
    full-context  every chunk in the repo, one general reviewer
    selective     retrieved top-K chunks, one general reviewer
    ensemble      retrieved top-K chunks, the four specialised reviewers

and reports how each one scored against a hand-labelled list of the issues
each PR really contains.

Matching is deterministic (file + line proximity + keyword overlap), not an
LLM judge, so the numbers are reproducible and cheap. That also makes them
conservative: a correct finding worded unusually can miss its label.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .llm import build_llm
from .models import Finding, ReviewError
from .pipeline import run_review

LINE_WINDOW = 15


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _as_text(value: Any) -> str:
    """Case files may write long strings as a list of lines for readability."""
    if isinstance(value, list):
        return "\n".join(str(v) for v in value)
    return str(value or "")

CONFIGS: dict[str, dict[str, Any]] = {
    # `diff-only` is genuinely context-free: no code chunks *and* no rules,
    # so it is a clean baseline rather than a partially-informed one.
    "diff-only": {"context_mode": "none", "max_rules": 0, "agents": ["general"]},
    "full-context": {"context_mode": "full", "agents": ["general"]},
    "selective": {"context_mode": "selective", "agents": ["general"]},
    "ensemble": {
        "context_mode": "selective",
        "agents": ["security", "correctness", "patterns", "requirements"],
    },
}


@dataclass
class Expected:
    file: str
    line: int
    category: str
    tags: list[str]

    def matches(self, finding: Finding) -> bool:
        if finding.file != self.file:
            return False
        if not (finding.start_line - LINE_WINDOW <= self.line <= finding.end_line + LINE_WINDOW):
            return False
        haystack = _normalise(f"{finding.title} {finding.description} {finding.evidence}")
        for tag in self.tags:
            needle = _normalise(tag)
            # Prefix match on a word boundary, so "authoris" catches
            # "authorisation" but "user" does not catch "superuser".
            if needle and re.search(rf"(?<![a-z0-9]){re.escape(needle)}", haystack):
                return True
        return False


@dataclass
class Case:
    id: str
    title: str
    diff: str
    task: str = ""
    expected: list[Expected] = field(default_factory=list)


@dataclass
class Score:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def add(self, other: "Score") -> None:
        self.tp += other.tp
        self.fp += other.fp
        self.fn += other.fn


def load_cases(cases_dir: Path) -> list[Case]:
    cases: list[Case] = []
    for path in sorted(cases_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ReviewError(f"{path}: invalid JSON: {exc}") from exc
        expected = [
            Expected(
                file=e["file"],
                line=int(e.get("line", 1)),
                category=e.get("category", ""),
                tags=list(e.get("tags", [])),
            )
            for e in data.get("expected", [])
        ]
        diff_text = _as_text(data.get("diff"))
        if not diff_text.strip():
            raise ReviewError(f"{path}: 'diff' is missing or empty")
        cases.append(
            Case(
                id=data.get("id", path.stem),
                title=data.get("title", path.stem),
                diff=diff_text if diff_text.endswith("\n") else diff_text + "\n",
                task=_as_text(data.get("task")),
                expected=expected,
            )
        )
    if not cases:
        raise ReviewError(f"no evaluation cases found in {cases_dir}")
    return cases


def score_case(findings: list[Finding], expected: list[Expected]) -> Score:
    """Greedy 1:1 matching between findings and labelled issues."""
    unmatched_expected = list(expected)
    score = Score()
    for finding in findings:
        hit = next((e for e in unmatched_expected if e.matches(finding)), None)
        if hit is not None:
            unmatched_expected.remove(hit)
            score.tp += 1
        else:
            score.fp += 1
    score.fn = len(unmatched_expected)
    return score


def _config_for(repo: Path, name: str, base: Config) -> Config:
    cfg = load_config(repo)
    cfg.model = base.model
    cfg.offline = base.offline
    cfg.max_tokens = base.max_tokens
    cfg.effort = base.effort
    cfg.history = False
    cfg.require_changed_lines = base.require_changed_lines
    cfg.min_confidence = base.min_confidence
    for key, value in CONFIGS[name].items():
        setattr(cfg, key, value)
    return cfg


def run_eval(
    repo: Path,
    cases_dir: Path,
    base_cfg: Config,
    config_names: list[str],
    limit: int | None = None,
) -> dict[str, Any]:
    cases = load_cases(cases_dir)
    if limit:
        cases = cases[:limit]

    totals: dict[str, Score] = {name: Score() for name in config_names}
    per_case: list[dict[str, Any]] = []
    # One config and one client per configuration, reused across cases.
    runtimes = {name: (_config_for(repo, name, base_cfg),) for name in config_names}
    runtimes = {name: (cfg, build_llm(cfg)) for name, (cfg,) in runtimes.items()}

    for case in cases:
        row: dict[str, Any] = {"id": case.id, "title": case.title, "expected": len(case.expected)}
        for name in config_names:
            cfg, llm = runtimes[name]
            try:
                result = run_review(cfg, llm, diff_text=case.diff, task=case.task or None)
                score = score_case(result.findings, case.expected)
            except ReviewError as exc:
                row[name] = {"error": str(exc)}
                continue
            totals[name].add(score)
            row[name] = {
                "found": len(result.findings),
                "tp": score.tp,
                "fp": score.fp,
                "fn": score.fn,
            }
        per_case.append(row)

    return {
        "cases": len(cases),
        "per_case": per_case,
        "aggregate": {
            name: {
                "precision": round(score.precision, 3),
                "recall": round(score.recall, 3),
                "f1": round(score.f1, 3),
                "tp": score.tp,
                "fp": score.fp,
                "fn": score.fn,
            }
            for name, score in totals.items()
        },
    }


def format_eval(report: dict[str, Any]) -> str:
    lines = [f"Evaluated {report['cases']} synthetic pull request(s).", ""]
    lines.append(f"{'config':<16}{'precision':>10}{'recall':>10}{'F1':>8}{'TP':>6}{'FP':>6}{'FN':>6}")
    lines.append("-" * 62)
    for name, agg in report["aggregate"].items():
        lines.append(
            f"{name:<16}{agg['precision']:>10.3f}{agg['recall']:>10.3f}{agg['f1']:>8.3f}"
            f"{agg['tp']:>6}{agg['fp']:>6}{agg['fn']:>6}"
        )
    lines.append("")
    lines.append("Per case (found / matched of expected):")
    for row in report["per_case"]:
        parts = []
        for name, agg in row.items():
            if name in ("id", "title", "expected"):
                continue
            if "error" in agg:
                parts.append(f"{name}=ERR")
            else:
                parts.append(f"{name}={agg['found']}/{agg['tp']}")
        lines.append(f"  {row['id']:<10} exp={row['expected']}  " + "  ".join(parts))
    return "\n".join(lines)
