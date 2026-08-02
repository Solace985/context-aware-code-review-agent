"""The context engine: chunk the repo, index it, retrieve only what matters.

The course lab showed the ranking that matters here: selective context beats
full context beats no context. Dumping the whole repository into the prompt
measurably *lowers* review quality, so retrieval is the product, not an
optimisation.

Retrieval is lexical (BM25 over identifier tokens) plus two structural
boosts, rather than embeddings. That is a deliberate trade for this project:

  * no second vendor, no second API key, no extra bytes of source leaving
    the machine just to build an index;
  * deterministic and unit-testable;
  * code-review queries are identifier-heavy, which is exactly the case where
    lexical matching is strong.

The cost is real: it will not match a renamed concept ("auth" vs "login")
the way embeddings would. See the README's "Honest limitations".
"""

from __future__ import annotations

import ast
import fnmatch
import math
import os
import re
from collections import Counter
from pathlib import Path

from .config import Config
from .models import Chunk, Diff, Rule
from .safety import is_sensitive_path, read_text_safely

SOURCE_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rb", ".java", ".kt", ".rs",
    ".cs", ".php", ".c", ".h", ".cc", ".cpp", ".hpp", ".swift", ".scala",
    ".sql", ".sh", ".yml", ".yaml", ".toml", ".md",
}

_WINDOW_LINES = 50
_WINDOW_OVERLAP = 10

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SUBTOKEN_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")

_CLIKE_DECL = re.compile(
    r"^[ \t]{0,4}(?:@[\w.]+\s*)?"
    r"(?:export\s+|default\s+|public\s+|private\s+|protected\s+|internal\s+|"
    r"static\s+|final\s+|abstract\s+|async\s+|pub\s+|open\s+)*"
    r"(?:function|class|interface|struct|enum|impl|trait|type|func|fn|def|"
    r"const|let|var|module|namespace)\s+([A-Za-z_$][\w$]*)"
)


def tokenize(text: str) -> list[str]:
    """Identifier-aware tokenizer: `getUserById` -> get, user, by, id."""
    out: list[str] = []
    for m in _TOKEN_RE.finditer(text):
        word = m.group(0)
        out.append(word.lower())
        for part in word.split("_"):
            for sub in _SUBTOKEN_RE.findall(part):
                if len(sub) > 2:
                    out.append(sub.lower())
    return out


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------


def _python_chunks(rel: str, text: str) -> list[Chunk] | None:
    """AST-based chunking for Python: one chunk per top-level def/class."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None

    lines = text.splitlines()
    chunks: list[Chunk] = []

    def emit(node: ast.AST, kind: str, name: str) -> None:
        start = getattr(node, "lineno", 1)
        end = getattr(node, "end_lineno", start) or start
        body = "\n".join(lines[start - 1 : end])
        if body.strip():
            chunks.append(Chunk(rel, kind, name, start, end, body))

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            emit(node, "function", node.name)
        elif isinstance(node, ast.ClassDef):
            emit(node, "class", node.name)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    emit(sub, "method", f"{node.name}.{sub.name}")

    if not chunks:
        return None

    # Keep module-level statements (imports, constants, app setup) reachable.
    covered = {ln for c in chunks for ln in range(c.start_line, c.end_line + 1)}
    header = [lines[i] for i in range(min(len(lines), 60)) if (i + 1) not in covered]
    header_text = "\n".join(header).strip()
    if header_text:
        chunks.insert(0, Chunk(rel, "module", Path(rel).stem, 1, min(len(lines), 60), header_text))
    return chunks


def _decl_chunks(rel: str, text: str) -> list[Chunk] | None:
    """Regex declaration slicing for C-like / JS / Go / Rust sources."""
    lines = text.splitlines()
    marks: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = _CLIKE_DECL.match(line)
        if m:
            marks.append((i, m.group(1)))
    if len(marks) < 2:
        return None

    chunks: list[Chunk] = []
    if marks[0][0] > 0:
        head = "\n".join(lines[: marks[0][0]]).strip()
        if head:
            chunks.append(Chunk(rel, "module", Path(rel).stem, 1, marks[0][0], head))
    for idx, (start_i, name) in enumerate(marks):
        end_i = marks[idx + 1][0] if idx + 1 < len(marks) else len(lines)
        body = "\n".join(lines[start_i:end_i]).strip()
        if body:
            chunks.append(Chunk(rel, "declaration", name, start_i + 1, end_i, body))
    return chunks


def _window_chunks(rel: str, text: str) -> list[Chunk]:
    lines = text.splitlines()
    chunks: list[Chunk] = []
    step = max(1, _WINDOW_LINES - _WINDOW_OVERLAP)
    for start in range(0, max(1, len(lines)), step):
        window = lines[start : start + _WINDOW_LINES]
        body = "\n".join(window).strip()
        if body:
            chunks.append(
                Chunk(rel, "window", f"L{start + 1}", start + 1, start + len(window), body)
            )
        if start + _WINDOW_LINES >= len(lines):
            break
    return chunks


def chunk_file(rel: str, text: str) -> list[Chunk]:
    suffix = Path(rel).suffix.lower()
    if suffix == ".py":
        chunks = _python_chunks(rel, text)
        if chunks:
            return chunks
    if suffix in {".js", ".jsx", ".ts", ".tsx", ".go", ".java", ".kt", ".rs", ".cs", ".php", ".swift", ".scala"}:
        chunks = _decl_chunks(rel, text)
        if chunks:
            return chunks
    return _window_chunks(rel, text)


# --------------------------------------------------------------------------
# Indexing + retrieval
# --------------------------------------------------------------------------


def excluded(rel: str, patterns: list[str]) -> bool:
    posix = rel.replace("\\", "/")
    for pat in patterns:
        if fnmatch.fnmatch(posix, pat) or fnmatch.fnmatch(f"/{posix}", pat):
            return True
        # `**/x/**` should also match a top-level `x/...`
        if pat.startswith("**/") and fnmatch.fnmatch(posix, pat[3:]):
            return True
    return False


_PRUNE_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
               "dist", "build", "target", "vendor", ".next", ".tox", ".mypy_cache",
               ".pytest_cache", ".ruff_cache", ".review-out"}


def iter_source_files(cfg: Config) -> tuple[list[str], list[str], bool]:
    """Walk the repo.

    Returns (relative source paths, skipped credential-like paths, hit_cap).
    Directories are pruned during the walk rather than filtered afterwards, so
    a repository with a large `node_modules` does not cost a full traversal.
    """
    root = cfg.root
    found: list[str] = []
    skipped: list[str] = []
    hit_cap = False

    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        rel_dir = current.relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in _PRUNE_DIRS
            and not (current / d).is_symlink()
            and not excluded(f"{rel_dir}/{d}" if rel_dir else d, cfg.exclude)
        )
        for name in sorted(filenames):
            if len(found) >= cfg.index_max_files:
                hit_cap = True
                return found, skipped, hit_cap
            path = current / name
            if path.is_symlink() or not path.is_file():
                continue
            rel = f"{rel_dir}/{name}" if rel_dir else name
            # Sensitivity is checked before the extension filter so the skip
            # is visible in the report rather than silently falling through.
            if is_sensitive_path(rel):
                skipped.append(rel)
                continue
            if path.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            if excluded(rel, cfg.exclude):
                continue
            found.append(rel)
    return found, skipped, hit_cap


class BM25Index:
    """Small BM25 index over chunk text. ~40 lines beats a vector DB here."""

    K1 = 1.5
    B = 0.75

    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks
        self.tokens: list[Counter[str]] = [Counter(tokenize(c.text + " " + c.path)) for c in chunks]
        self.lengths = [sum(t.values()) or 1 for t in self.tokens]
        self.avg_len = sum(self.lengths) / len(self.lengths) if self.lengths else 1.0
        self.df: Counter[str] = Counter()
        for tf in self.tokens:
            self.df.update(tf.keys())
        self.n = len(chunks)

    def score(self, query: list[str]) -> list[float]:
        scores = [0.0] * self.n
        if not self.n:
            return scores
        qcount = Counter(query)
        for term, qtf in qcount.items():
            df = self.df.get(term, 0)
            if df == 0:
                continue
            idf = math.log(1 + (self.n - df + 0.5) / (df + 0.5))
            weight = idf * min(qtf, 3)
            for i, tf in enumerate(self.tokens):
                f = tf.get(term, 0)
                if not f:
                    continue
                denom = f + self.K1 * (1 - self.B + self.B * self.lengths[i] / self.avg_len)
                scores[i] += weight * (f * (self.K1 + 1)) / denom
        return scores


def build_index(cfg: Config) -> tuple[BM25Index, list[str], list[str], bool]:
    files, skipped, hit_cap = iter_source_files(cfg)
    chunks: list[Chunk] = []
    for rel in files:
        text = read_text_safely(cfg.root, rel, cfg.index_max_file_bytes)
        if text is None:
            continue
        chunks.extend(chunk_file(rel, text))
    return BM25Index(chunks), files, skipped, hit_cap


def retrieve(
    index: BM25Index,
    query: list[str],
    diff: Diff,
    top_k: int,
    max_chunk_chars: int,
) -> list[Chunk]:
    """Top-K chunks, BM25 plus structural boosts.

    Structural boosts encode what a human reviewer does when they open a PR:
    look at neighbours of the changed files, and look at whatever defines the
    symbols the change touches.
    """
    if not index.chunks or top_k <= 0:
        return []

    scores = index.score(query)
    changed = set(diff.paths)
    changed_dirs = {str(Path(p).parent) for p in changed}
    changed_stems = {Path(p).stem.lower() for p in changed}

    ranked: list[tuple[float, Chunk]] = []
    for i, chunk in enumerate(index.chunks):
        score = scores[i]
        if score <= 0:
            continue
        if chunk.path in changed:
            # The diff already shows these lines; nearby code is mildly useful,
            # a duplicate of the diff is not.
            score *= 0.55
        if str(Path(chunk.path).parent) in changed_dirs:
            score *= 1.25
        if chunk.name and chunk.name.split(".")[-1].lower() in changed_stems:
            score *= 1.15
        if chunk.kind in ("function", "method", "class", "declaration"):
            score *= 1.10
        ranked.append((score, chunk))

    ranked.sort(key=lambda sc: (-sc[0], sc[1].path, sc[1].start_line))

    selected: list[Chunk] = []
    seen_files: Counter[str] = Counter()
    for _, chunk in ranked:
        if len(selected) >= top_k:
            break
        # Cap per-file dominance so one big module cannot eat the whole budget.
        if seen_files[chunk.path] >= 3:
            continue
        seen_files[chunk.path] += 1
        text = chunk.text
        if len(text) > max_chunk_chars:
            text = text[:max_chunk_chars].rstrip() + "\n… [chunk truncated]"
        selected.append(
            Chunk(chunk.path, chunk.kind, chunk.name, chunk.start_line, chunk.end_line, text)
        )
    return selected


def all_chunks(index: BM25Index, max_chunk_chars: int, budget: int = 400) -> list[Chunk]:
    """`context.mode: full` — every chunk, so the eval can show why that is worse."""
    out: list[Chunk] = []
    for chunk in index.chunks[:budget]:
        text = chunk.text
        if len(text) > max_chunk_chars:
            text = text[:max_chunk_chars].rstrip() + "\n… [chunk truncated]"
        out.append(Chunk(chunk.path, chunk.kind, chunk.name, chunk.start_line, chunk.end_line, text))
    return out


# --------------------------------------------------------------------------
# Rules & task context
# --------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def load_rules(cfg: Config) -> list[Rule]:
    """Read `.review/rules/*.md`. Optional YAML frontmatter sets id/severity."""
    import yaml  # local import keeps module import cheap for tests

    rules_dir = cfg.root / cfg.rules_dir
    if not rules_dir.is_dir():
        return []
    rules: list[Rule] = []
    for path in sorted(rules_dir.glob("*.md")):
        rel = path.relative_to(cfg.root).as_posix()
        text = read_text_safely(cfg.root, rel, cfg.index_max_file_bytes)
        if not text:
            continue
        rule_id = path.stem
        severity = "medium"
        title = path.stem.replace("-", " ").replace("_", " ").title()
        m = _FRONTMATTER_RE.match(text)
        if m:
            try:
                meta = yaml.safe_load(m.group(1)) or {}
            except Exception:
                meta = {}
            if isinstance(meta, dict):
                rule_id = str(meta.get("id", rule_id))
                severity = str(meta.get("severity", severity)).lower()
                if meta.get("title"):
                    title = str(meta["title"])
            text = text[m.end() :]
        for line in text.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
        rules.append(Rule(id=rule_id, title=title, path=rel, severity=severity, text=text.strip()))
    return rules


def select_rules(rules: list[Rule], query: list[str], diff: Diff, limit: int) -> list[Rule]:
    """Retrieve the rules relevant to *this* change, not the whole handbook."""
    if not rules or limit <= 0:
        return []
    if len(rules) <= limit:
        return rules
    pseudo = [Chunk(r.path, "rule", r.id, 1, 1, f"{r.id} {r.title}\n{r.text}") for r in rules]
    index = BM25Index(pseudo)
    scores = index.score(query)
    order = sorted(range(len(rules)), key=lambda i: -scores[i])
    picked = [rules[i] for i in order[:limit]]
    return sorted(picked, key=lambda r: r.id)


def load_task(cfg: Config, task: str | None) -> tuple[str, str]:
    """`--task` accepts a file path or literal text. Returns (text, source)."""
    if not task:
        return "", ""
    candidate = Path(task)
    if not candidate.is_absolute():
        candidate = cfg.root / task
    if candidate.is_file():
        try:
            rel = candidate.resolve().relative_to(cfg.root.resolve()).as_posix()
            text = read_text_safely(cfg.root, rel, 256 * 1024)
            source = rel
        except ValueError:
            # A task file outside the repo is fine — it is user-supplied, not
            # repo content — but read it directly rather than via safe_resolve,
            # and report only its name so a local directory layout does not
            # end up published in a PR comment.
            text = candidate.read_text(encoding="utf-8", errors="replace")[: 256 * 1024]
            source = candidate.name
        if text:
            return text.strip(), source
    return task.strip(), "inline"
