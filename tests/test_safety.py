import pytest

from codereview.models import ReviewError
from codereview.safety import (
    fence_for,
    is_sensitive_path,
    read_text_safely,
    redact,
    safe_resolve,
    sanitize_model_text,
)


@pytest.mark.parametrize(
    "secret",
    [
        'ANTHROPIC_API_KEY = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz012345"',
        "aws_key = 'AKIAIOSFODNN7EXAMPLE'",
        "token = ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        'password = "hunter2hunter2"',
        "url = 'postgres://admin:s3cretpw@db.internal:5432/app'",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\n-----END RSA PRIVATE KEY-----",
        "auth = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w'",
    ],
)
def test_secrets_are_redacted(secret):
    clean, count = redact(secret)
    assert count >= 1
    assert "REDACTED" in clean


def test_redaction_leaves_ordinary_code_alone():
    code = "def add(a, b):\n    return a + b\n"
    clean, count = redact(code)
    assert count == 0
    assert clean == code


@pytest.mark.parametrize(
    "path",
    [".env", ".env.production", "config/id_rsa", "certs/server.pem", "app/credentials.json", ".npmrc"],
)
def test_sensitive_paths_are_recognised(path):
    assert is_sensitive_path(path)


@pytest.mark.parametrize("path", ["app/users.py", "src/env.ts", "docs/environment.md"])
def test_ordinary_paths_are_not_sensitive(path):
    assert not is_sensitive_path(path)


@pytest.mark.parametrize("evil", ["../../etc/passwd", "..\\..\\windows\\win.ini", "sub/../../outside.txt"])
def test_path_traversal_is_refused(tmp_path, evil):
    with pytest.raises(ReviewError, match="outside the repository"):
        safe_resolve(tmp_path, evil)


def test_safe_resolve_allows_paths_inside_the_repo(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1")
    assert safe_resolve(tmp_path, "pkg/mod.py").is_file()


def test_read_text_safely_refuses_secret_files(tmp_path):
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-real")
    assert read_text_safely(tmp_path, ".env", 10_000) is None


def test_read_text_safely_refuses_binary_and_oversized(tmp_path):
    (tmp_path / "blob.py").write_bytes(b"\x00\x01\x02binary")
    assert read_text_safely(tmp_path, "blob.py", 10_000) is None
    (tmp_path / "big.py").write_text("x" * 5000)
    assert read_text_safely(tmp_path, "big.py", 100) is None


def test_sanitize_strips_control_chars_and_neutralises_html_comments():
    out = sanitize_model_text("hello\x07 <!-- hidden --> world")
    assert "\x07" not in out
    assert "<!--" not in out and "-->" not in out


def test_sanitize_caps_length():
    assert len(sanitize_model_text("a" * 10_000, max_len=100)) <= 120


def test_fence_outgrows_embedded_backticks():
    assert fence_for("plain") == "```"
    assert fence_for("a ``` b") == "````"
    assert fence_for("````") == "`````"
