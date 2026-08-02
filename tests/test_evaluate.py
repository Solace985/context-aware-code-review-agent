from pathlib import Path

from codereview.evaluate import Expected, load_cases, score_case
from codereview.models import Finding

EVALS = Path(__file__).resolve().parents[1] / "evals"


def finding(**kwargs) -> Finding:
    base = dict(
        title="SQL injection in the search query",
        category="security",
        severity="high",
        confidence=0.9,
        file="app/users.py",
        start_line=39,
        end_line=39,
        description="The term is interpolated straight into the statement.",
        evidence="f\"... LIKE '%{term}%'\"",
        suggestion="Bind it.",
    )
    base.update(kwargs)
    return Finding(**base)


def test_expected_matches_on_file_line_and_keyword():
    expected = Expected("app/users.py", 39, "security", ["sql", "injection"])
    assert expected.matches(finding())


def test_expected_rejects_a_different_file():
    expected = Expected("app/other.py", 39, "security", ["sql"])
    assert not expected.matches(finding())


def test_expected_rejects_a_distant_line():
    expected = Expected("app/users.py", 400, "security", ["sql"])
    assert not expected.matches(finding())


def test_expected_rejects_an_unrelated_finding_on_the_same_line():
    expected = Expected("app/users.py", 39, "security", ["path traversal"])
    assert not expected.matches(finding(title="Missing docstring", description="", evidence=""))


def test_multiword_tags_are_normalised():
    expected = Expected("app/users.py", 39, "security", ["assert_owner"])
    assert expected.matches(finding(description="The handler never calls assert_owner on the row."))


def test_prefix_tags_match_word_variants():
    expected = Expected("app/users.py", 39, "security", ["authoris"])
    assert expected.matches(finding(description="No authorisation check is performed."))


def test_score_case_counts_tp_fp_fn():
    expected = [
        Expected("app/users.py", 39, "security", ["sql"]),
        Expected("app/users.py", 35, "security", ["admin"]),
    ]
    findings = [finding(), finding(title="Unrelated nit", description="", evidence="")]
    score = score_case(findings, expected)
    assert (score.tp, score.fp, score.fn) == (1, 1, 1)
    assert round(score.precision, 2) == 0.5
    assert round(score.recall, 2) == 0.5
    assert round(score.f1, 2) == 0.5


def test_one_finding_cannot_claim_two_labels():
    expected = [
        Expected("app/users.py", 39, "security", ["sql"]),
        Expected("app/users.py", 39, "security", ["sql"]),
    ]
    score = score_case([finding()], expected)
    assert (score.tp, score.fn) == (1, 1)


def test_shipped_cases_load_and_are_well_formed():
    cases = load_cases(EVALS / "cases")
    assert len(cases) >= 5
    for case in cases:
        assert case.diff.startswith("diff --git"), case.id
        assert case.expected, case.id
        for exp in case.expected:
            assert exp.tags, f"{case.id}: {exp.file} has no tags"
            assert exp.line > 0


def test_shipped_case_expectations_point_at_lines_the_diff_touches():
    from codereview.diff import parse_diff

    for case in load_cases(EVALS / "cases"):
        diff = parse_diff(case.diff)
        for exp in case.expected:
            changed = diff.by_path(exp.file)
            assert changed is not None, f"{case.id}: {exp.file} is not in the diff"
            near = {
                ln
                for ln in changed.added_line_numbers
                if abs(ln - exp.line) <= 15
            }
            assert near, f"{case.id}: expected line {exp.line} in {exp.file} is not near a changed line"
