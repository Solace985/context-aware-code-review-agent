"""Orchestration: diff -> context -> parallel agents -> merged, triaged result.

The filtering in `_validate` is what separates this from pasting a diff into a
chat window. A raw model reply routinely contains findings anchored to files
that are not in the change, or to line numbers that do not exist, or hedged
observations dressed up as defects. Those are dropped here, before a human
ever sees them, and the count of what was dropped is reported.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import context as ctx_engine
from .agents import Agent, FINDINGS_SCHEMA, select_agents, system_prompt
from .config import Config
from .diff import (
    MAX_HUNK_LINES,
    apply_limits,
    diff_query_terms,
    get_diff_text,
    line_is_changed,
    parse_diff,
)
from .models import (
    CATEGORIES,
    SEVERITIES,
    AgentRun,
    ChangedFile,
    Diff,
    Finding,
    ReviewContext,
    ReviewError,
    ReviewResult,
    severity_rank,
)
from .safety import redact, sanitize_model_text


# --------------------------------------------------------------------------
# Prompt rendering
# --------------------------------------------------------------------------


def render_diff(diff: Diff) -> str:
    """Render the diff with explicit NEW-file line numbers.

    Models cite line numbers badly when asked to count `@@` offsets themselves,
    and a wrong line number makes a finding unusable. Numbering every line
    removes the arithmetic.
    """
    out: list[str] = []
    for f in diff.files:
        header = f"### FILE: {f.path} ({f.status}"
        if f.old_path and f.old_path != f.path:
            header += f", was {f.old_path}"
        out.append(header + ")")
        if f.binary:
            out.append("    [binary file, contents not shown]")
            out.append("")
            continue
        if not f.hunks:
            out.append("    [no textual hunks]")
            out.append("")
            continue
        for hunk in f.hunks:
            out.append(f"@@ new lines {hunk.new_start}-{hunk.new_start + hunk.new_count - 1} @@")
            for kind, lineno, text in hunk.lines[:MAX_HUNK_LINES]:
                if kind == "-":
                    out.append(f"       - {text}")
                else:
                    out.append(f"{lineno:>6} {kind} {text}")
            if len(hunk.lines) > MAX_HUNK_LINES:
                out.append(f"       … [{len(hunk.lines) - MAX_HUNK_LINES} more lines in this hunk]")
        out.append("")
    return "\n".join(out)


def render_context(rc: ReviewContext) -> str:
    if not rc.chunks:
        return "(no repository context was retrieved)"
    parts: list[str] = []
    for chunk in rc.chunks:
        parts.append(f"--- {chunk.path}:{chunk.start_line}-{chunk.end_line} [{chunk.kind} {chunk.name}]")
        parts.append(chunk.text)
        parts.append("")
    return "\n".join(parts)


def render_rules(rc: ReviewContext) -> str:
    if not rc.rules:
        return "(this repository has no written rules)"
    parts: list[str] = []
    for rule in rc.rules:
        parts.append(f"--- rule id: {rule.id} | severity: {rule.severity} | source: {rule.path}")
        parts.append(rule.text)
        parts.append("")
    return "\n".join(parts)


def build_user_message(diff: Diff, rc: ReviewContext) -> str:
    sections = [
        "Review the following change.",
        "",
        "<diff>",
        render_diff(diff),
        "</diff>",
        "",
        "<repository_rules>",
        render_rules(rc),
        "</repository_rules>",
        "",
        "<repository_context>",
        render_context(rc),
        "</repository_context>",
    ]
    if rc.task:
        sections += ["", "<task>", rc.task, "</task>"]
    if diff.truncated:
        sections += [
            "",
            "NOTE: this diff was truncated to fit a size limit. Do not comment on "
            "anything you cannot see.",
        ]
    sections += [
        "",
        "Return your findings using the required JSON schema. An empty list is a "
        "valid and often correct answer.",
    ]
    return "\n".join(sections)


# --------------------------------------------------------------------------
# Context assembly
# --------------------------------------------------------------------------


def build_context(cfg: Config, diff: Diff, task: str | None) -> ReviewContext:
    rc = ReviewContext(mode=cfg.context_mode)
    rc.task, rc.task_source = ctx_engine.load_task(cfg, task)

    rules = ctx_engine.load_rules(cfg)
    query = diff_query_terms(diff)
    if rules:
        query_tokens = [t for term in query for t in ctx_engine.tokenize(term)]
        rc.rules = ctx_engine.select_rules(rules, query_tokens, diff, cfg.max_rules)

    if cfg.context_mode != "none":
        index, files, skipped, hit_cap = ctx_engine.build_index(cfg)
        rc.indexed_files = len(files)
        rc.indexed_chunks = len(index.chunks)
        rc.skipped_sensitive = skipped
        rc.index_capped = hit_cap
        if cfg.context_mode == "full":
            rc.chunks = ctx_engine.all_chunks(index, cfg.max_chunk_chars)
        else:
            query_tokens = [t for term in query for t in ctx_engine.tokenize(term)]
            rc.chunks = ctx_engine.retrieve(
                index, query_tokens, diff, cfg.max_chunks, cfg.max_chunk_chars
            )

    # Redact before anything crosses the network.
    total = 0
    rc.task, n = redact(rc.task)
    total += n
    for chunk in rc.chunks:
        chunk.text, n = redact(chunk.text)
        total += n
    for rule in rc.rules:
        rule.text, n = redact(rule.text)
        total += n
    rc.redactions = total
    return rc


def redact_diff(diff: Diff) -> int:
    """Scrub secrets out of a parsed diff, in place."""
    total = 0
    for f in diff.files:
        for hunk in f.hunks:
            hunk.added = [(ln, redact(t)[0]) for ln, t in hunk.added]
            new_lines = []
            for kind, ln, text in hunk.lines:
                clean, n = redact(text)
                total += n
                new_lines.append((kind, ln, clean))
            hunk.lines = new_lines
    return total


# --------------------------------------------------------------------------
# Validation, dedupe, ranking
# --------------------------------------------------------------------------


def _coerce_finding(raw: Any, agent_name: str) -> Finding | None:
    if not isinstance(raw, dict):
        return None
    title = sanitize_model_text(raw.get("title"), 200)
    if not title:
        return None
    try:
        start = int(raw.get("start_line") or 0)
        end = int(raw.get("end_line") or start)
    except (TypeError, ValueError):
        return None
    if end < start:
        start, end = end, start
    try:
        confidence = float(raw.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    severity = str(raw.get("severity", "")).strip().lower()
    if severity not in SEVERITIES:
        severity = "medium"
    category = str(raw.get("category", "")).strip().lower()
    if category not in CATEGORIES:
        category = "correctness"

    rule_ids_raw = raw.get("rule_ids") or []
    rule_ids = (
        [sanitize_model_text(r, 80) for r in rule_ids_raw if isinstance(r, str)][:8]
        if isinstance(rule_ids_raw, list)
        else []
    )

    return Finding(
        title=title,
        category=category,
        severity=severity,
        confidence=confidence,
        file=sanitize_model_text(raw.get("file"), 400).lstrip("/"),
        start_line=max(0, start),
        end_line=max(0, end),
        description=sanitize_model_text(raw.get("description"), 2000),
        evidence=sanitize_model_text(raw.get("evidence"), 1200),
        suggestion=sanitize_model_text(raw.get("suggestion"), 2000),
        rule_ids=[r for r in rule_ids if r],
        found_by=[agent_name],
    )


def _validate(
    findings: list[Finding],
    diff: Diff,
    rc: ReviewContext,
    cfg: Config,
    dropped: dict[str, int],
) -> list[Finding]:
    """Reject findings we cannot trace back to the change."""
    known_paths = {f.path: f for f in diff.files}
    known_rules = {r.id for r in rc.rules}
    kept: list[Finding] = []

    for finding in findings:
        changed_file: ChangedFile | None = known_paths.get(finding.file)
        if changed_file is None:
            # Tolerate a path rendered with a leading directory the model added.
            matches = [p for p in known_paths if p.endswith("/" + finding.file) or finding.file.endswith("/" + p)]
            if len(matches) == 1:
                finding.file = matches[0]
                changed_file = known_paths[matches[0]]
        if changed_file is None:
            dropped["file_not_in_diff"] = dropped.get("file_not_in_diff", 0) + 1
            continue

        if finding.confidence < cfg.min_confidence:
            dropped["low_confidence"] = dropped.get("low_confidence", 0) + 1
            continue

        if cfg.require_changed_lines and not line_is_changed(
            changed_file, finding.start_line, finding.end_line, cfg.changed_line_slack
        ):
            dropped["outside_changed_lines"] = dropped.get("outside_changed_lines", 0) + 1
            continue

        if not finding.description:
            dropped["no_description"] = dropped.get("no_description", 0) + 1
            continue

        unknown = [r for r in finding.rule_ids if r not in known_rules]
        if unknown:
            # Keep the finding, drop the unverifiable citation.
            finding.rule_ids = [r for r in finding.rule_ids if r in known_rules]
            dropped["unknown_rule_citation"] = dropped.get("unknown_rule_citation", 0) + len(unknown)

        kept.append(finding)
    return kept


def _title_tokens(title: str) -> set[str]:
    return {w for w in "".join(c.lower() if c.isalnum() else " " for c in title).split() if len(w) > 2}


def _similar(a: Finding, b: Finding) -> bool:
    if a.file != b.file:
        return False
    if max(a.start_line, b.start_line) > min(a.end_line, b.end_line) + 6:
        return False
    ta, tb = _title_tokens(a.title), _title_tokens(b.title)
    if not ta or not tb:
        return False
    overlap = len(ta & tb) / len(ta | tb)
    return overlap >= 0.45 or (a.category == b.category and overlap >= 0.3)


def _merge(findings: list[Finding], dropped: dict[str, int]) -> list[Finding]:
    """Collapse the same defect reported by several agents.

    Agreement between independent specialists is real signal, so a merged
    finding gets a confidence bump rather than just being deduplicated away.
    """
    merged: list[Finding] = []
    for finding in findings:
        for existing in merged:
            if _similar(existing, finding):
                if severity_rank(finding.severity) < severity_rank(existing.severity):
                    existing.severity = finding.severity
                    existing.title = finding.title
                    existing.description = finding.description
                existing.rule_ids = sorted(set(existing.rule_ids) | set(finding.rule_ids))
                for name in finding.found_by:
                    if name not in existing.found_by:
                        existing.found_by.append(name)
                        # Independent agreement is signal, but it is not proof;
                        # keep the bump small and never claim certainty.
                        existing.confidence = min(0.98, existing.confidence + 0.05)
                existing.start_line = min(existing.start_line, finding.start_line)
                existing.end_line = max(existing.end_line, finding.end_line)
                if len(finding.suggestion) > len(existing.suggestion):
                    existing.suggestion = finding.suggestion
                dropped["duplicate"] = dropped.get("duplicate", 0) + 1
                break
        else:
            merged.append(finding)
    return merged


def _rank(findings: list[Finding], limit: int, dropped: dict[str, int]) -> list[Finding]:
    findings.sort(key=lambda f: (severity_rank(f.severity), -f.confidence, f.file, f.start_line))
    if len(findings) > limit:
        dropped["over_max_findings"] = dropped.get("over_max_findings", 0) + (len(findings) - limit)
        findings = findings[:limit]
    return findings


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def run_review(
    cfg: Config,
    llm: Any,
    base: str | None = None,
    staged: bool = False,
    diff_file: str | None = None,
    diff_text: str | None = None,
    task: str | None = None,
) -> ReviewResult:
    started = time.monotonic()
    result = ReviewResult(model=getattr(llm, "model", cfg.model))

    raw = diff_text if diff_text is not None else get_diff_text(cfg.root, base, staged, diff_file)
    diff = parse_diff(raw)
    diff.files = [f for f in diff.files if not ctx_engine.excluded(f.path, cfg.exclude)]
    diff = apply_limits(diff, cfg.max_changed_files, cfg.max_diff_bytes)
    result.diff = diff

    if not diff.files:
        result.warnings.append("No reviewable changes were found.")
        result.elapsed_s = time.monotonic() - started
        return result

    if diff.dropped_files:
        result.warnings.append(
            f"{len(diff.dropped_files)} file(s) exceeded limits.max_changed_files / "
            f"limits.max_diff_bytes and were NOT reviewed: "
            f"{', '.join(diff.dropped_files[:10])}"
            + (" …" if len(diff.dropped_files) > 10 else "")
        )
    if diff.truncated_hunks:
        result.warnings.append(
            f"{diff.truncated_hunks} hunk(s) were longer than {MAX_HUNK_LINES} lines; "
            f"only the first {MAX_HUNK_LINES} lines of each were reviewed."
        )

    rc = build_context(cfg, diff, task)
    rc.redactions += redact_diff(diff)
    result.context = rc

    if rc.redactions:
        result.warnings.append(
            f"{rc.redactions} secret-shaped value(s) were redacted before the request was sent."
        )
    if rc.skipped_sensitive:
        result.warnings.append(
            f"Skipped {len(rc.skipped_sensitive)} credential-like file(s) when indexing: "
            + ", ".join(rc.skipped_sensitive[:5])
        )
    if rc.index_capped:
        result.warnings.append(
            f"Indexing stopped at context.index_max_files ({cfg.index_max_files}); "
            f"parts of the repository were not available for retrieval."
        )

    agents = select_agents(cfg.agents, has_task=bool(rc.task))
    if not agents:
        raise ReviewError("no agents selected (a `requirements`-only run needs --task)")

    user_message = build_user_message(diff, rc)

    def run_agent(agent: Agent) -> tuple[AgentRun, list[Finding]]:
        run = AgentRun(name=agent.name, ok=False)
        try:
            reply = llm.complete_json(system_prompt(agent), user_message, FINDINGS_SCHEMA)
        except ReviewError as exc:
            run.error = str(exc)
            return run, []
        except Exception as exc:  # pragma: no cover - provider surprises
            run.error = f"{type(exc).__name__}: {exc}"
            return run, []
        run.ok = True
        run.input_tokens = reply.input_tokens
        run.output_tokens = reply.output_tokens
        raw_list = reply.data.get("findings")
        if not isinstance(raw_list, list):
            run.error = "reply had no 'findings' array"
            run.ok = False
            return run, []
        run.raw_findings = len(raw_list)
        parsed = [_coerce_finding(item, agent.name) for item in raw_list]
        return run, [f for f in parsed if f is not None]

    collected: list[Finding] = []
    with ThreadPoolExecutor(max_workers=min(4, len(agents))) as pool:
        for run, findings in pool.map(run_agent, agents):
            result.agents.append(run)
            collected.extend(findings)

    if all(not a.ok for a in result.agents):
        errors = "; ".join(f"{a.name}: {a.error}" for a in result.agents if a.error)
        raise ReviewError(f"every reviewer failed. {errors}")
    for agent_run in result.agents:
        if not agent_run.ok:
            result.warnings.append(f"Reviewer '{agent_run.name}' failed: {agent_run.error}")

    dropped: dict[str, int] = {}
    malformed = sum(a.raw_findings for a in result.agents) - len(collected)
    if malformed > 0:
        dropped["malformed_reply_item"] = malformed

    validated = _validate(collected, diff, rc, cfg, dropped)
    merged = _merge(validated, dropped)
    result.findings = _rank(merged, cfg.max_findings, dropped)
    result.dropped = dropped
    result.elapsed_s = time.monotonic() - started
    return result


def gate_failed(result: ReviewResult, fail_on: str) -> bool:
    """True when the review should fail the build."""
    if fail_on == "none":
        return False
    threshold = severity_rank(fail_on)
    return any(severity_rank(f.severity) <= threshold for f in result.findings)


def append_history(cfg: Config, result: ReviewResult) -> None:
    """Record finding metadata locally so `codereview codify` can spot patterns.

    Titles and rule ids only — no source code, no diff. The file is gitignored
    by `codereview init` because even titles can leak internal detail.
    """
    if not cfg.history or not result.findings:
        return
    path = cfg.root / ".review" / "history.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "model": result.model,
            "findings": [
                {
                    "title": f.title,
                    "category": f.category,
                    "severity": f.severity,
                    "file": f.file,
                    "rule_ids": f.rule_ids,
                }
                for f in result.findings
            ],
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # history is a convenience, never a reason to fail a review


def estimate_prompt(cfg: Config, llm: Any, diff: Diff, rc: ReviewContext) -> dict[str, int]:
    """Token counts per agent for `--estimate`, using the provider's counter."""
    user_message = build_user_message(diff, rc)
    totals: dict[str, int] = {}
    for agent in select_agents(cfg.agents, has_task=bool(rc.task)):
        totals[agent.name] = llm.count_tokens(system_prompt(agent), user_message)
    return totals


def load_last_review(out_dir: Path) -> dict[str, Any]:
    path = out_dir / "review.json"
    if not path.is_file():
        raise ReviewError(f"no previous review found at {path}. Run `codereview review` first.")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReviewError(f"{path} is not valid JSON: {exc}") from exc
