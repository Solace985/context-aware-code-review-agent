import json

from codereview.models import AgentRun, Chunk, Finding, ReviewContext, ReviewResult, Rule
from codereview.report import to_json, to_markdown, to_sarif, to_terminal, write_outputs
from codereview.diff import parse_diff


def sample_result() -> ReviewResult:
    return ReviewResult(
        model="claude-opus-5",
        elapsed_s=3.2,
        diff=parse_diff(
            "diff --git a/app/x.py b/app/x.py\n--- a/app/x.py\n+++ b/app/x.py\n@@ -1 +1,2 @@\n+bad()\n"
        ),
        context=ReviewContext(
            mode="selective",
            indexed_files=12,
            indexed_chunks=88,
            chunks=[Chunk("app/y.py", "function", "helper", 4, 20, "...")],
            rules=[Rule("no-raw-sql", "Never build SQL by hand", ".review/rules/sql.md", "high", "body")],
            task_source="pr-body.md",
        ),
        agents=[AgentRun("security", True, 2, "", 100, 20)],
        dropped={"file_not_in_diff": 2},
        warnings=["1 secret-shaped value was redacted."],
        findings=[
            Finding(
                title="SQL built from a request value",
                category="security",
                severity="critical",
                confidence=0.9,
                file="app/x.py",
                start_line=1,
                end_line=1,
                description="An attacker controls the query.",
                evidence="query(f\"... {term}\")",
                suggestion="Use bound parameters.",
                rule_ids=["no-raw-sql"],
                found_by=["security", "patterns"],
            ),
            Finding(
                title="Docstring missing",
                category="maintainability",
                severity="low",
                confidence=0.6,
                file="app/x.py",
                start_line=1,
                end_line=1,
                description="Neighbouring functions all have one.",
                evidence="def bad():",
                suggestion="Add one.",
                found_by=["patterns"],
            ),
        ],
    )


def test_markdown_separates_triage_buckets():
    md = to_markdown(sample_result())
    assert "Action required (1)" in md
    assert "Nitpicks (1)" in md
    assert "Review recommended" not in md  # nothing in that bucket


def test_markdown_shows_evidence_and_rule_traceability():
    md = to_markdown(sample_result())
    assert "Evidence" in md
    assert "`no-raw-sql`" in md
    assert "Never build SQL by hand" in md
    assert "pr-body.md" in md
    assert "found by security, patterns" in md


def test_markdown_reports_what_was_filtered_out():
    assert "file not in diff: 2" in to_markdown(sample_result())


def test_markdown_cannot_be_broken_out_of_by_backticks_in_evidence():
    result = sample_result()
    result.findings[0].evidence = "```\n## Fake heading injected by the model\n```"
    md = to_markdown(result)
    assert "````" in md  # the fence grew to contain it


def test_markdown_is_capped_for_github():
    from codereview.report import MAX_MARKDOWN_CHARS

    result = sample_result()
    result.findings = result.findings * 400
    md = to_markdown(result)
    assert len(md) <= MAX_MARKDOWN_CHARS + 200


def test_markdown_with_no_findings():
    result = sample_result()
    result.findings = []
    assert "**No findings.**" in to_markdown(result)


def test_json_is_machine_readable():
    data = json.loads(to_json(sample_result()))
    assert data["counts"]["critical"] == 1
    assert data["findings"][0]["triage"] == "action_required"
    assert data["context_used"]["rules"][0]["id"] == "no-raw-sql"
    assert data["dropped"]["file_not_in_diff"] == 2


def test_sarif_shape_is_valid_enough_for_github():
    data = json.loads(to_sarif(sample_result()))
    assert data["version"] == "2.1.0"
    run = data["runs"][0]
    assert run["tool"]["driver"]["name"] == "code-review-agent"
    result = run["results"][0]
    assert result["level"] == "error"
    region = result["locations"][0]["physicalLocation"]["region"]
    assert region["startLine"] >= 1


def test_terminal_output_is_plain_text():
    text = to_terminal(sample_result())
    assert "ACTION REQUIRED (1)" in text
    assert "app/x.py:1" in text


def test_write_outputs_creates_requested_files(tmp_path):
    written = write_outputs(sample_result(), tmp_path / "out", ["markdown", "json", "sarif"])
    names = {p.name for p in written}
    assert names == {"review.md", "review.json", "review.sarif"}
    for path in written:
        assert path.read_text(encoding="utf-8")
