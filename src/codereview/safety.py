"""Security controls applied to everything before it can leave the machine.

Three jobs:
  1. Refuse to read files that are secrets by nature (.env, *.pem, keystores).
  2. Redact secret-shaped strings out of any text bound for the model.
  3. Keep all file access inside the repository root, and keep model output
     from escaping the Markdown/JSON we embed it in.

The threat model is deliberately blunt: the diff, the repo and the ticket are
all UNTRUSTED input, and the model's reply is UNTRUSTED output. Nothing here
executes anything.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from .models import ReviewError

# --------------------------------------------------------------------------
# 1. Files we never open, regardless of config
# --------------------------------------------------------------------------

SENSITIVE_PATH_PATTERNS = (
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.tfstate",
    "*.tfstate.backup",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "credentials",
    "credentials.*",
    "secrets.*",
    "*secrets.y*ml",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".htpasswd",
    "*.kdbx",
)


def is_sensitive_path(rel_path: str) -> bool:
    """True if a path looks like it holds credentials rather than code."""
    name = Path(rel_path).name
    lowered = name.lower()
    for pat in SENSITIVE_PATH_PATTERNS:
        if fnmatch.fnmatch(lowered, pat.lower()):
            return True
    return False


# --------------------------------------------------------------------------
# 2. Secret redaction
# --------------------------------------------------------------------------

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key", re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----")),
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}")),
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA)[0-9A-Z]{16}\b")),
    ("aws-secret", re.compile(r"(?i)aws[_\-]?secret[_\-]?access[_\-]?key\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{40}")),
    ("gcp-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("stripe-key", re.compile(r"\b(?:sk|rk)_live_[0-9a-zA-Z]{16,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("url-credentials", re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:@/]+:[^\s:@/]+@")),
    (
        "assigned-secret",
        re.compile(
            r"(?i)\b(?:password|passwd|pwd|secret|api[_\-]?key|apikey|access[_\-]?token|"
            r"auth[_\-]?token|client[_\-]?secret|private[_\-]?token|session[_\-]?key)\b"
            r"\s*[:=]\s*['\"][^'\"\n]{8,}['\"]"
        ),
    ),
)

_REDACTION = "<<REDACTED:{name}>>"


def redact(text: str) -> tuple[str, int]:
    """Replace secret-shaped substrings. Returns (clean_text, n_redactions)."""
    if not text:
        return text, 0
    total = 0
    for name, pattern in SECRET_PATTERNS:
        text, n = pattern.subn(_REDACTION.format(name=name), text)
        total += n
    return text, total


# --------------------------------------------------------------------------
# 3. Path containment
# --------------------------------------------------------------------------


def safe_resolve(root: Path, rel_path: str) -> Path:
    """Resolve `rel_path` under `root`, refusing anything that escapes it.

    Guards against `../` traversal, absolute paths and symlinks that point
    outside the repository. Raises ReviewError rather than returning a path
    the caller might use anyway.
    """
    root = root.resolve()
    candidate = (root / rel_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ReviewError(f"refusing to access path outside the repository: {rel_path}") from exc
    return candidate


def read_text_safely(root: Path, rel_path: str, max_bytes: int) -> str | None:
    """Read a repo-relative text file, or None if it is unreadable/unsuitable."""
    if is_sensitive_path(rel_path):
        return None
    path = safe_resolve(root, rel_path)
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > max_bytes:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:  # binary
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


# --------------------------------------------------------------------------
# 4. Output sanitisation (model reply -> our files)
# --------------------------------------------------------------------------

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_model_text(text: object, max_len: int = 4000) -> str:
    """Make untrusted model output safe to embed in Markdown / JSON / SARIF."""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    text = _CONTROL_CHARS.sub("", text).strip()
    # A stray HTML comment or tag can hide content in a rendered PR comment.
    text = text.replace("<!--", "&lt;!--").replace("-->", "--&gt;")
    if len(text) > max_len:
        text = text[:max_len].rstrip() + " …[truncated]"
    return text


def fence_for(text: str) -> str:
    """Pick a code-fence long enough that `text` cannot break out of it."""
    longest = 0
    run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    return "`" * max(3, longest + 1)
