"""Getting a diff out of git and parsing it into structured hunks.

The parsed line numbers are what later lets us reject findings the model
invented for code that is not actually in the change.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .models import ChangedFile, Diff, Hunk, ReviewError

_GIT_TIMEOUT = 60

# Refs are passed to git as argv, never through a shell, but a ref that starts
# with '-' would still be read as a flag. Keep the accepted shape narrow.
_SAFE_REF = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/\-^~@{}]*$")

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _run_git(root: Path, args: list[str]) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            # Decode explicitly. `text=True` would use the locale codec, which
            # on a cp1252 Windows console cannot decode a UTF-8 diff — and the
            # resulting decode error happens on subprocess's reader thread, so
            # `stdout` silently comes back as None instead of raising.
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment dependent
        raise ReviewError("git is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:  # pragma: no cover
        raise ReviewError("git command timed out") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        hint = detail[0] if detail else f"exit {proc.returncode}"
        raise ReviewError(f"git {' '.join(args[:2])} failed: {hint}")
    return proc.stdout or ""


def is_git_repo(root: Path) -> bool:
    try:
        out = _run_git(root, ["rev-parse", "--is-inside-work-tree"])
    except ReviewError:
        return False
    return out.strip() == "true"


def _validate_ref(ref: str) -> str:
    if not _SAFE_REF.match(ref):
        raise ReviewError(
            f"refusing to use unsafe git ref {ref!r}; expected something like 'main' or 'origin/main'"
        )
    return ref


def resolve_base(root: Path, base: str) -> str:
    """Return a revision to diff against, preferring the merge base."""
    _validate_ref(base)
    candidates = [base]
    if "/" not in base:
        candidates.append(f"origin/{base}")
    for cand in candidates:
        try:
            _run_git(root, ["rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}"])
        except ReviewError:
            continue
        try:
            merge_base = _run_git(root, ["merge-base", cand, "HEAD"]).strip()
            if merge_base:
                return merge_base
        except ReviewError:
            pass
        return cand
    raise ReviewError(
        f"base ref '{base}' not found. Fetch it first (git fetch origin {base}) "
        f"or pass a different --base."
    )


def get_diff_text(
    root: Path,
    base: str | None = None,
    staged: bool = False,
    diff_file: str | None = None,
) -> str:
    """Produce the unified diff to review.

    * `diff_file` — read a diff from disk (used by CI and the eval harness).
    * `staged`    — review what is staged for commit.
    * `base`      — review this branch against the merge base with `base`,
                    including uncommitted work-tree changes (pre-PR review).
    """
    if diff_file:
        path = Path(diff_file)
        if not path.is_file():
            raise ReviewError(f"diff file not found: {diff_file}")
        return path.read_text(encoding="utf-8", errors="replace")

    if not is_git_repo(root):
        raise ReviewError(f"{root} is not a git repository (use --diff-file to review a patch)")

    common = ["--no-color", "--unified=3", "--find-renames", "--no-ext-diff"]
    if staged:
        return _run_git(root, ["diff", "--cached", *common])
    if base:
        rev = resolve_base(root, base)
        return _run_git(root, ["diff", *common, rev, "--"])
    return _run_git(root, ["diff", *common, "HEAD", "--"])


def _strip_prefix(path: str) -> str:
    if path.startswith(('a/', 'b/')):
        return path[2:]
    return path


def _unquote(path: str) -> str:
    # git quotes paths containing unusual bytes: "a/we\303\251ird.py"
    if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
        inner = path[1:-1]
        try:
            # Undo the C-style escapes, then read the resulting bytes as UTF-8.
            return inner.encode("utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            return inner
    return path


def parse_diff(text: str) -> Diff:
    """Parse a unified diff into ChangedFile/Hunk objects."""
    diff = Diff(raw=text)
    current: ChangedFile | None = None
    hunk: Hunk | None = None
    new_lineno = 0
    buf: list[str] = []

    def flush() -> None:
        nonlocal current, hunk, buf
        if current is not None:
            current.raw = "\n".join(buf)
            if current.hunks or current.binary or current.status in ("added", "deleted", "renamed"):
                diff.files.append(current)
        current, hunk, buf = None, None, []

    for line in text.splitlines():
        if line.startswith("diff --git "):
            flush()
            current = ChangedFile(path="")
            buf = [line]
            rest = line[len("diff --git ") :]
            # Best effort split of "a/x b/x"; git quotes paths with spaces.
            if " b/" in rest:
                a_part, b_part = rest.split(" b/", 1)
                current.old_path = _strip_prefix(_unquote(a_part))
                current.path = _unquote("b/" + b_part)[2:]
            continue

        if current is None:
            continue
        buf.append(line)

        if line.startswith("@@"):
            m = _HUNK_RE.match(line)
            if not m:
                continue
            new_start = int(m.group(3))
            new_count = int(m.group(4) or 1)
            hunk = Hunk(new_start=new_start, new_count=new_count)
            current.hunks.append(hunk)
            new_lineno = new_start
            continue

        # Inside a hunk, every line is content. Checking the file headers
        # first would misread a removed `--- ` line (YAML front matter, a
        # Markdown rule) or an added `+++ ` line as a new file header.
        if hunk is not None:
            if line.startswith("+"):
                hunk.added.append((new_lineno, line[1:]))
                hunk.lines.append(("+", new_lineno, line[1:]))
                new_lineno += 1
            elif line.startswith("-"):
                hunk.removed.append(line[1:])
                hunk.lines.append(("-", 0, line[1:]))
            elif line.startswith("\\"):
                pass  # "\ No newline at end of file"
            else:
                hunk.lines.append((" ", new_lineno, line[1:] if line else ""))
                new_lineno += 1
            continue

        # Pre-hunk header lines.
        if line.startswith("new file mode"):
            current.status = "added"
        elif line.startswith("deleted file mode"):
            current.status = "deleted"
        elif line.startswith("rename from "):
            current.status = "renamed"
            current.old_path = _strip_prefix(_unquote(line[len("rename from ") :]))
        elif line.startswith("rename to "):
            current.status = "renamed"
            current.path = _strip_prefix(_unquote(line[len("rename to ") :]))
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            current.binary = True
        elif line.startswith("--- "):
            src = _unquote(line[4:].strip())
            if src != "/dev/null":
                current.old_path = _strip_prefix(src)
        elif line.startswith("+++ "):
            dst = _unquote(line[4:].strip())
            if dst == "/dev/null":
                current.status = "deleted"
                if current.old_path:
                    current.path = current.old_path
            else:
                current.path = _strip_prefix(dst)

    flush()
    return diff


MAX_HUNK_LINES = 400


def apply_limits(diff: Diff, max_files: int, max_bytes: int) -> Diff:
    """Bound the diff we send. Anything dropped is recorded, never silent.

    Whole files are dropped rather than the byte stream being cut mid-hunk, so
    what survives here is exactly what the prompt renders — a truncated tail
    that the reviewers never see must not still be listed as reviewed.
    """
    dropped: list[str] = []

    if len(diff.files) > max_files:
        # Keep the smallest files: one huge generated file should not crowd
        # out ten small hand-written ones.
        by_size = sorted(diff.files, key=lambda f: len(f.raw))
        keep_ids = {id(f) for f in by_size[:max_files]}
        dropped += [f.path for f in diff.files if id(f) not in keep_ids]
        diff.files = [f for f in diff.files if id(f) in keep_ids]

    kept: list[ChangedFile] = []
    used = 0
    for f in diff.files:
        size = len(f.raw.encode("utf-8")) + 1
        if kept and used + size > max_bytes:
            dropped.append(f.path)
            continue
        kept.append(f)
        used += size
    diff.files = kept

    if dropped:
        diff.dropped_files = sorted(set(dropped))
        diff.truncated = True

    diff.truncated_hunks = sum(
        1 for f in diff.files for h in f.hunks if len(h.lines) > MAX_HUNK_LINES
    )
    if diff.truncated_hunks:
        diff.truncated = True

    diff.raw = "\n".join(f.raw for f in diff.files)
    return diff


_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")

_STOPWORDS = {
    "self", "this", "true", "false", "none", "null", "return", "import", "from",
    "const", "let", "var", "def", "class", "function", "public", "private",
    "static", "async", "await", "and", "not", "for", "the", "with", "int", "str",
}


def diff_query_terms(diff: Diff, limit: int = 120) -> list[str]:
    """Identifiers touched by the change — the retrieval query."""
    counts: dict[str, int] = {}
    for f in diff.files:
        for part in re.split(r"[/\\.]", f.path):
            if len(part) > 2:
                counts[part.lower()] = counts.get(part.lower(), 0) + 3
        for hunk in f.hunks:
            for _, text in hunk.added:
                for m in _IDENT_RE.finditer(text):
                    tok = m.group(0).lower()
                    if tok not in _STOPWORDS:
                        counts[tok] = counts.get(tok, 0) + 1
            for text in hunk.removed:
                for m in _IDENT_RE.finditer(text):
                    tok = m.group(0).lower()
                    if tok not in _STOPWORDS:
                        counts[tok] = counts.get(tok, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [tok for tok, _ in ranked[:limit]]


def line_is_changed(f: ChangedFile, start: int, end: int, slack: int) -> bool:
    """Does [start, end] overlap (within `slack`) any line this diff added?"""
    if f.status == "deleted" or f.binary:
        return True  # nothing to anchor to; let other filters decide
    if not f.hunks:
        return True
    lo, hi = min(start, end) - slack, max(start, end) + slack
    return any(lo <= ln <= hi for ln in f.added_line_numbers)
