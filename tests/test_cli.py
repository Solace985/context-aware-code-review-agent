import json
import os
import subprocess

import pytest

from codereview.cli import main

DIFF = """\
diff --git a/app/service.py b/app/service.py
index 1111111..2222222 100644
--- a/app/service.py
+++ b/app/service.py
@@ -1,3 +1,5 @@
 def handler(payload):
     log(payload)
+    eval(payload)
+    return True
     return False
"""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEREVIEW_OFFLINE", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "service.py").write_text("def handler(payload):\n    log(payload)\n", encoding="utf-8")
    (tmp_path / "change.diff").write_text(DIFF, encoding="utf-8")
    return tmp_path


def test_init_scaffolds_and_is_idempotent(tmp_path, capsys):
    assert main(["--repo", str(tmp_path), "init"]) == 0
    assert (tmp_path / ".review.yml").is_file()
    assert (tmp_path / ".review" / "rules").is_dir()
    assert (tmp_path / ".github" / "workflows" / "ai-code-review.yml").is_file()
    assert ".review-out/" in (tmp_path / ".gitignore").read_text()

    (tmp_path / ".review.yml").write_text("model: mine\n", encoding="utf-8")
    assert main(["--repo", str(tmp_path), "init"]) == 0
    assert (tmp_path / ".review.yml").read_text() == "model: mine\n"
    assert "exists" in capsys.readouterr().out


def test_init_workflow_uses_pull_request_not_pull_request_target(tmp_path):
    main(["--repo", str(tmp_path), "init"])
    workflow = (tmp_path / ".github" / "workflows" / "ai-code-review.yml").read_text()
    # Comments explain why pull_request_target is wrong; the YAML must not use it.
    directives = "\n".join(
        line for line in workflow.splitlines() if not line.lstrip().startswith("#")
    )
    assert "pull_request_target" not in directives
    assert "pull-requests: write" in workflow
    assert "contents: read" in workflow
    # PR body must be read via the event payload, not shell-interpolated.
    assert "GITHUB_EVENT_PATH" in workflow
    assert "${{ github.event.pull_request.body }}" not in workflow


def test_review_offline_writes_all_formats_and_gates(repo):
    out = repo / "out"
    code = main(
        [
            "--repo", str(repo), "review",
            "--diff-file", str(repo / "change.diff"),
            "--offline",
            "--format", "markdown", "--format", "json", "--format", "sarif",
            "--out", str(out),
        ]
    )
    assert code == 1  # the stub flags eval() as critical, fail_on defaults to high
    data = json.loads((out / "review.json").read_text(encoding="utf-8"))
    assert any("eval" in f["title"] for f in data["findings"])
    assert (out / "review.md").is_file()
    assert (out / "review.sarif").is_file()


def test_review_exit_zero_when_gate_disabled(repo):
    code = main(
        ["--repo", str(repo), "review", "--diff-file", str(repo / "change.diff"),
         "--offline", "--fail-on", "none", "--out", str(repo / "out"), "--quiet"]
    )
    assert code == 0


def test_review_with_no_changes_succeeds(repo):
    (repo / "empty.diff").write_text("", encoding="utf-8")
    code = main(
        ["--repo", str(repo), "review", "--diff-file", str(repo / "empty.diff"),
         "--offline", "--out", str(repo / "out")]
    )
    assert code == 0


def test_missing_api_key_is_a_clear_error(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CODEREVIEW_OFFLINE", raising=False)
    (tmp_path / "c.diff").write_text(DIFF, encoding="utf-8")
    code = main(["--repo", str(tmp_path), "review", "--diff-file", str(tmp_path / "c.diff")])
    assert code == 2
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().err


def test_bad_config_exits_two(repo, capsys):
    (repo / ".review.yml").write_text("api_key: sk-ant-oops\n", encoding="utf-8")
    code = main(["--repo", str(repo), "review", "--diff-file", str(repo / "change.diff"), "--offline"])
    assert code == 2
    assert "not allowed" in capsys.readouterr().err


def test_unknown_agent_exits_two(repo, capsys):
    code = main(
        ["--repo", str(repo), "review", "--diff-file", str(repo / "change.diff"),
         "--offline", "--agents", "wizardry"]
    )
    assert code == 2
    assert "unknown agent" in capsys.readouterr().err


def test_ask_requires_a_previous_review(repo, capsys):
    code = main(["--repo", str(repo), "ask", "why?", "--offline", "--out", str(repo / "nowhere")])
    assert code == 2
    assert "no previous review" in capsys.readouterr().err


def test_ask_reads_the_last_review(repo, capsys):
    out = repo / "out"
    main(
        ["--repo", str(repo), "review", "--diff-file", str(repo / "change.diff"),
         "--offline", "--format", "json", "--out", str(out), "--quiet"]
    )
    code = main(["--repo", str(repo), "ask", "what is the risk?", "--offline", "--out", str(out)])
    assert code == 0
    assert "stub" in capsys.readouterr().out.lower()


def test_codify_without_history_exits_two(repo, capsys):
    code = main(["--repo", str(repo), "codify", "--offline"])
    assert code == 2
    assert "no review history" in capsys.readouterr().err


def test_estimate_makes_no_review_call(repo, capsys):
    code = main(
        ["--repo", str(repo), "review", "--diff-file", str(repo / "change.diff"),
         "--offline", "--estimate"]
    )
    assert code == 0
    assert "input tokens" in capsys.readouterr().out


@pytest.mark.skipif(not os.environ.get("PATH"), reason="needs a shell environment")
def test_review_against_a_real_git_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEREVIEW_OFFLINE", "1")

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    try:
        git("init", "-q")
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.skip("git is not available")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "Test")
    (tmp_path / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    git("branch", "-M", "main")
    git("checkout", "-q", "-b", "feature")
    (tmp_path / "mod.py").write_text("def f():\n    eval('1')\n    return 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "change")

    out = tmp_path / "out"
    code = main(
        ["--repo", str(tmp_path), "review", "--base", "main", "--offline",
         "--format", "json", "--out", str(out), "--quiet"]
    )
    assert code == 1
    data = json.loads((out / "review.json").read_text(encoding="utf-8"))
    assert data["changed_files"][0]["path"] == "mod.py"


def test_a_diff_containing_non_ascii_is_read_correctly(tmp_path, monkeypatch):
    """git output is UTF-8; decoding it with the locale codec loses the diff."""
    monkeypatch.setenv("CODEREVIEW_OFFLINE", "1")

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    try:
        git("init", "-q")
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.skip("git is not available")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "Test")
    (tmp_path / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "base")

    # Em dash, arrow and emoji — all multi-byte in UTF-8, all undecodable in cp1252.
    (tmp_path / "mod.py").write_text(
        'VALUE = 2  # cost — latency → ✅ \U0001f534\nNAME = "café"\n', encoding="utf-8"
    )

    from codereview.diff import get_diff_text, parse_diff

    text = get_diff_text(tmp_path, base=None, staged=False, diff_file=None)
    assert isinstance(text, str) and text
    diff = parse_diff(text)
    assert diff.paths == ["mod.py"]
    assert any("café" in line for _, line in diff.files[0].hunks[0].added)


def test_missing_base_ref_is_a_clear_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CODEREVIEW_OFFLINE", "1")
    try:
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.skip("git is not available")
    code = main(["--repo", str(tmp_path), "review", "--base", "nonexistent-branch", "--offline"])
    assert code == 2
    assert "not found" in capsys.readouterr().err
