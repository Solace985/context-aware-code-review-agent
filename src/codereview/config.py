"""Loading and validating `.review.yml`.

Config controls behaviour only. It is explicitly *not* allowed to carry
credentials: a key that looks like a secret is a hard error, so nobody can
accidentally commit an API key into a repo-tracked file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import SEVERITIES, ReviewError

CONFIG_FILENAME = ".review.yml"

DEFAULT_EXCLUDES = [
    "**/.git/**",
    "**/node_modules/**",
    "**/.venv/**",
    "**/venv/**",
    "**/__pycache__/**",
    "**/dist/**",
    "**/build/**",
    "**/target/**",
    "**/vendor/**",
    "**/.next/**",
    "**/coverage/**",
    "**/*.min.js",
    "**/*.lock",
    "**/*.snap",
    "**/*.svg",
    "**/*.map",
    "**/.review-out/**",
    # Rules are injected into the prompt separately; indexing them as source
    # would spend the retrieval budget showing the model the same text twice.
    "**/.review/**",
]

ALL_AGENTS = ("security", "correctness", "patterns", "requirements")

# Keys that must never appear in a checked-in config file. The check is a
# substring match and deliberately fails closed, so legitimate settings whose
# name happens to contain one of these words are allowlisted by hand.
_FORBIDDEN_KEY_TOKENS = (
    "api_key", "apikey", "token", "secret", "password", "passwd", "credential", "auth",
)
_ALLOWED_KEYS = {"max_tokens"}


@dataclass
class Config:
    # model / provider
    model: str = "claude-opus-5"
    max_tokens: int = 16000
    effort: str | None = None  # low|medium|high|xhigh|max — None uses the API default
    timeout_s: float = 180.0
    max_retries: int = 3

    # agents
    agents: list[str] = field(default_factory=lambda: list(ALL_AGENTS))

    # context engine
    context_mode: str = "selective"  # selective | full | none
    max_chunks: int = 12
    max_chunk_chars: int = 1800
    max_rules: int = 6
    index_max_files: int = 400
    index_max_file_kb: int = 200

    # finding filtering / triage
    min_confidence: float = 0.5
    max_findings: int = 15
    require_changed_lines: bool = True
    changed_line_slack: int = 3
    fail_on: str = "high"  # critical|high|medium|low|none

    # hard limits
    max_diff_bytes: int = 200_000
    max_changed_files: int = 60

    # paths
    rules_dir: str = ".review/rules"
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    history: bool = True

    # runtime-only (never from file)
    root: Path = field(default_factory=Path.cwd)
    offline: bool = False

    @property
    def index_max_file_bytes(self) -> int:
        return self.index_max_file_kb * 1024


def _fail(msg: str) -> None:
    raise ReviewError(f"{CONFIG_FILENAME}: {msg}")


def _check_no_secrets(data: dict[str, Any], prefix: str = "") -> None:
    for key, value in data.items():
        flat = f"{prefix}{key}"
        normalised = str(key).lower().replace("-", "_")
        if normalised not in _ALLOWED_KEYS and any(tok in normalised for tok in _FORBIDDEN_KEY_TOKENS):
            _fail(
                f"key '{flat}' is not allowed. Credentials must come from the "
                f"environment (ANTHROPIC_API_KEY), never from a config file."
            )
        if isinstance(value, dict):
            _check_no_secrets(value, prefix=f"{flat}.")


def _as_int(section: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    raw = section.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int):
        _fail(f"'{key}' must be an integer")
    return max(lo, min(hi, int(raw)))


def _as_float(section: dict[str, Any], key: str, default: float, lo: float, hi: float) -> float:
    raw = section.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        _fail(f"'{key}' must be a number")
    return max(lo, min(hi, float(raw)))


def _as_bool(section: dict[str, Any], key: str, default: bool) -> bool:
    raw = section.get(key, default)
    if not isinstance(raw, bool):
        _fail(f"'{key}' must be true or false")
    return raw


def load_config(root: Path, overrides: dict[str, Any] | None = None) -> Config:
    """Read `.review.yml` from `root` (optional) and apply CLI overrides."""
    root = Path(root).resolve()
    cfg = Config(root=root)

    path = root / CONFIG_FILENAME
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            # safe_load only: never construct arbitrary Python objects from a
            # file that may have arrived with a pull request.
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            _fail(f"could not be parsed: {exc}")
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            _fail("top level must be a mapping")
        data = loaded
        _check_no_secrets(data)

    if "model" in data:
        model = data["model"]
        if not isinstance(model, str) or not model.strip():
            _fail("'model' must be a non-empty string")
        cfg.model = model.strip()

    if "effort" in data and data["effort"] is not None:
        effort = str(data["effort"]).lower()
        if effort not in ("low", "medium", "high", "xhigh", "max"):
            _fail("'effort' must be one of: low, medium, high, xhigh, max")
        cfg.effort = effort

    cfg.max_tokens = _as_int(data, "max_tokens", cfg.max_tokens, 1024, 64000)
    cfg.timeout_s = _as_float(data, "timeout_s", cfg.timeout_s, 10.0, 900.0)
    cfg.max_retries = _as_int(data, "max_retries", cfg.max_retries, 0, 8)

    if "agents" in data:
        agents = data["agents"]
        if not isinstance(agents, list) or not all(isinstance(a, str) for a in agents):
            _fail("'agents' must be a list of strings")
        unknown = [a for a in agents if a not in ALL_AGENTS]
        if unknown:
            _fail(f"unknown agent(s): {', '.join(unknown)}. Valid: {', '.join(ALL_AGENTS)}")
        if not agents:
            _fail("'agents' must not be empty")
        cfg.agents = list(dict.fromkeys(agents))

    ctx = data.get("context") or {}
    if not isinstance(ctx, dict):
        _fail("'context' must be a mapping")
    mode = str(ctx.get("mode", cfg.context_mode)).lower()
    if mode not in ("selective", "full", "none"):
        _fail("'context.mode' must be selective, full or none")
    cfg.context_mode = mode
    cfg.max_chunks = _as_int(ctx, "max_chunks", cfg.max_chunks, 0, 100)
    cfg.max_chunk_chars = _as_int(ctx, "max_chunk_chars", cfg.max_chunk_chars, 200, 20000)
    cfg.max_rules = _as_int(ctx, "max_rules", cfg.max_rules, 0, 50)
    cfg.index_max_files = _as_int(ctx, "index_max_files", cfg.index_max_files, 1, 5000)
    cfg.index_max_file_kb = _as_int(ctx, "index_max_file_kb", cfg.index_max_file_kb, 1, 2048)

    rev = data.get("review") or {}
    if not isinstance(rev, dict):
        _fail("'review' must be a mapping")
    cfg.min_confidence = _as_float(rev, "min_confidence", cfg.min_confidence, 0.0, 1.0)
    cfg.max_findings = _as_int(rev, "max_findings", cfg.max_findings, 1, 200)
    cfg.require_changed_lines = _as_bool(rev, "require_changed_lines", cfg.require_changed_lines)
    cfg.changed_line_slack = _as_int(rev, "changed_line_slack", cfg.changed_line_slack, 0, 50)
    cfg.history = _as_bool(rev, "history", cfg.history)
    fail_on = str(rev.get("fail_on", cfg.fail_on)).lower()
    if fail_on not in (*SEVERITIES, "none"):
        _fail("'review.fail_on' must be critical, high, medium, low or none")
    cfg.fail_on = fail_on

    lim = data.get("limits") or {}
    if not isinstance(lim, dict):
        _fail("'limits' must be a mapping")
    cfg.max_diff_bytes = _as_int(lim, "max_diff_bytes", cfg.max_diff_bytes, 1000, 2_000_000)
    cfg.max_changed_files = _as_int(lim, "max_changed_files", cfg.max_changed_files, 1, 1000)

    if "rules_dir" in data:
        rules_dir = data["rules_dir"]
        if not isinstance(rules_dir, str):
            _fail("'rules_dir' must be a string")
        if Path(rules_dir).is_absolute() or ".." in Path(rules_dir).parts:
            _fail("'rules_dir' must be a relative path inside the repository")
        cfg.rules_dir = rules_dir

    if "exclude" in data:
        exclude = data["exclude"]
        if not isinstance(exclude, list) or not all(isinstance(p, str) for p in exclude):
            _fail("'exclude' must be a list of glob strings")
        cfg.exclude = list(DEFAULT_EXCLUDES) + exclude

    for key, value in (overrides or {}).items():
        if value is None:
            continue
        if not hasattr(cfg, key):
            raise ReviewError(f"internal: unknown config override '{key}'")
        setattr(cfg, key, value)

    if os.environ.get("CODEREVIEW_OFFLINE") == "1":
        cfg.offline = True

    return cfg


DEFAULT_CONFIG_YAML = """\
# code-review-agent configuration.
# Behaviour only — credentials come from the ANTHROPIC_API_KEY environment
# variable and are rejected if they appear here.
version: 1

model: claude-opus-5
max_tokens: 16000
# effort: high        # low|medium|high|xhigh|max (Claude Opus 5 / Sonnet 5)

# Specialised reviewers that run in parallel and are then merged.
# `requirements` only runs when you pass a task/ticket with --task.
agents:
  - security
  - correctness
  - patterns
  - requirements

context:
  mode: selective     # selective (recommended) | full | none
  max_chunks: 12      # top-K repository chunks retrieved per review
  max_chunk_chars: 1800
  max_rules: 6

review:
  min_confidence: 0.5
  max_findings: 15
  require_changed_lines: true   # drop findings that do not touch the diff
  changed_line_slack: 3
  fail_on: high                 # exit 1 when a finding is this severe or worse
  history: true                 # append finding summaries to .review/history.jsonl

limits:
  max_diff_bytes: 200000
  max_changed_files: 60

rules_dir: .review/rules

exclude:
  - "**/*.generated.*"
"""
