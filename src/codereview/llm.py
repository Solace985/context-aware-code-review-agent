"""The only module that talks to the model provider.

Rules enforced here:
  * the API key is read from the process environment and nowhere else;
  * it is never echoed, logged, or written to an output file;
  * the model's reply is parsed and validated, never executed;
  * requests are bounded by an explicit timeout with bounded retries.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

from .config import Config
from .models import ReviewError

API_KEY_ENV = "ANTHROPIC_API_KEY"

_JSON_FALLBACK_INSTRUCTION = (
    "\nOUTPUT FORMAT\n"
    "Reply with a single JSON object and nothing else - no prose, no code "
    "fence. It must validate against this JSON Schema:\n"
)

_SCHEMA_REJECTION_MARKERS = ("output_config", "output_format", "json_schema", "structured output")


def _is_schema_rejection(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _SCHEMA_REJECTION_MARKERS)


@dataclass
class LLMResponse:
    data: dict[str, Any]
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


def _extract_json(text: str) -> dict[str, Any]:
    """Parse a JSON object out of a model reply, tolerating stray prose."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    if start == -1:
        raise ReviewError("model reply contained no JSON object")
    depth, in_str, escaped = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[start : i + 1])
                except json.JSONDecodeError as exc:
                    raise ReviewError(f"model reply was not valid JSON: {exc}") from exc
                if not isinstance(parsed, dict):
                    raise ReviewError("model reply was not a JSON object")
                return parsed
    raise ReviewError("model reply contained an unterminated JSON object")


class AnthropicLLM:
    """Thin wrapper over the Anthropic Messages API."""

    def __init__(self, cfg: Config):
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ReviewError("the 'anthropic' package is not installed: pip install anthropic") from exc

        api_key = os.environ.get(API_KEY_ENV, "").strip()
        if not api_key:
            raise ReviewError(
                f"{API_KEY_ENV} is not set.\n"
                f"  export {API_KEY_ENV}=sk-ant-...   (or add it to a gitignored .env and source it)\n"
                f"Or run with --offline to use the deterministic stub reviewer."
            )

        self._anthropic = anthropic
        self.cfg = cfg
        self.model = cfg.model
        # Set once if the configured model turns out to predate structured
        # outputs; subsequent calls then skip straight to the prose fallback.
        self._schema_unsupported = False
        self.client = anthropic.Anthropic(
            api_key=api_key,
            timeout=cfg.timeout_s,
            max_retries=cfg.max_retries,
        )

    # -- request plumbing -------------------------------------------------

    def _output_config(self, schema: dict[str, Any] | None) -> dict[str, Any] | None:
        out: dict[str, Any] = {}
        if schema is not None:
            out["format"] = {"type": "json_schema", "schema": schema}
        if self.cfg.effort:
            out["effort"] = self.cfg.effort
        return out or None

    def _create(self, kwargs: dict[str, Any]) -> Any:
        """Call the SDK, moving newer params to extra_body on older SDKs."""
        try:
            return self.client.messages.create(**kwargs)
        except TypeError as exc:
            movable = {k: kwargs.pop(k) for k in ("output_config",) if k in kwargs}
            if not movable:
                raise ReviewError(f"unsupported request for this SDK version: {exc}") from exc
            extra = dict(kwargs.pop("extra_body", {}) or {})
            extra.update(movable)
            kwargs["extra_body"] = extra
            return self.client.messages.create(**kwargs)

    def _call(
        self,
        system: str,
        user: str,
        schema: dict[str, Any] | None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        if schema is not None and self._schema_unsupported:
            # This model rejected structured outputs on an earlier call; ask
            # for the same shape in prose and parse it defensively instead.
            system = f"{system}\n{_JSON_FALLBACK_INSTRUCTION}\n{json.dumps(schema)}"

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or self.cfg.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        output_config = self._output_config(None if self._schema_unsupported else schema)
        if output_config:
            kwargs["output_config"] = output_config

        try:
            response = self._create(kwargs)
        except self._anthropic.BadRequestError as exc:
            message = str(getattr(exc, "message", "") or exc)
            if schema is None or self._schema_unsupported or not _is_schema_rejection(message):
                raise ReviewError(f"API rejected the request (400): {message}") from exc
            # Older models have no structured-output support. Fall back once,
            # remember it, and keep going rather than failing the review.
            self._schema_unsupported = True
            return self._call(system, user, schema, max_tokens)
        except self._anthropic.AuthenticationError as exc:
            raise ReviewError(f"{API_KEY_ENV} was rejected by the API (401).") from exc
        except self._anthropic.PermissionDeniedError as exc:
            raise ReviewError(f"API key lacks access to model '{self.model}' (403).") from exc
        except self._anthropic.NotFoundError as exc:
            raise ReviewError(f"model '{self.model}' not found. Check `model:` in .review.yml.") from exc
        except self._anthropic.RateLimitError as exc:
            raise ReviewError("rate limited by the API after retries; try again shortly.") from exc
        except self._anthropic.APIStatusError as exc:
            raise ReviewError(f"API error {exc.status_code}: {exc.message}") from exc
        except self._anthropic.APIConnectionError as exc:
            raise ReviewError(f"could not reach the API: {exc}") from exc

        # Opus 5 can decline a request; content is then empty or partial.
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) or "unspecified"
            raise ReviewError(f"the model declined this request (category: {category}).")

        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        usage = getattr(response, "usage", None)
        result = LLMResponse(
            data={},
            text=text,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
        )
        if schema is not None:
            if getattr(response, "stop_reason", None) == "max_tokens":
                # A truncated JSON reply is unparseable; say why rather than
                # letting the JSON error surface.
                raise ReviewError(
                    "reply hit max_tokens before finishing. Raise `max_tokens` in "
                    ".review.yml, or lower `context.max_chunks` / `review.max_findings`."
                )
            result.data = _extract_json(text)
        return result

    # -- public API -------------------------------------------------------

    def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> LLMResponse:
        return self._call(system, user, schema)

    def complete_text(self, system: str, user: str, max_tokens: int = 4000) -> LLMResponse:
        return self._call(system, user, None, max_tokens=max_tokens)

    def count_tokens(self, system: str, user: str) -> int:
        try:
            result = self.client.messages.count_tokens(
                model=self.model,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return int(result.input_tokens)
        except Exception:  # pragma: no cover - estimation is best effort
            return 0


# --------------------------------------------------------------------------
# Offline stub
# --------------------------------------------------------------------------

_STUB_PATTERNS: tuple[tuple[re.Pattern[str], str, str, str], ...] = (
    (re.compile(r"\beval\s*\("), "Use of eval() on the changed line", "security", "critical"),
    (
        re.compile(r"\bos\.system\s*\(|shell\s*=\s*True"),
        "Shell command built in changed code",
        "security",
        "critical",
    ),
    (
        re.compile(
            r"(?:execute|query)\s*\(\s*f[\"']|(?:select|insert|update|delete)\b[^\"'\n]*[\"']\s*\+",
            re.IGNORECASE,
        ),
        "Possible SQL string interpolation",
        "security",
        "high",
    ),
    (re.compile(r"verify\s*=\s*False"), "TLS verification disabled", "security", "high"),
    (re.compile(r"except\s*:\s*$|except\s+Exception\s*:\s*$"), "Exception possibly swallowed", "reliability", "medium"),
    (
        re.compile(r"\b(?:password|api_key|secret|token)\s*=\s*[\"'][^\"']{6,}[\"']", re.IGNORECASE),
        "Hardcoded credential",
        "security",
        "critical",
    ),
    (re.compile(r"\bpickle\.loads?\s*\("), "Unsafe deserialisation", "security", "high"),
    (re.compile(r"\bTODO\b|\bFIXME\b"), "Unresolved TODO added", "maintainability", "low"),
)


class StubLLM:
    """Deterministic offline reviewer.

    This is a smoke-test harness, not a review. It pattern-matches a handful of
    obvious problems so the pipeline, filters, report and exit codes can be
    exercised end-to-end without an API key or a bill. It has no understanding
    of the code and will miss almost everything a real review would find.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.model = "offline-stub"

    def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> LLMResponse:
        findings: list[dict[str, Any]] = []
        current_file = ""
        line_no = 0
        for raw in user.splitlines():
            m = re.match(r"^### FILE: (.+?)(?: \(|$)", raw)
            if m:
                current_file = m.group(1).strip()
                continue
            m = re.match(r"^\s*(\d+)\s*\+\s?(.*)$", raw)
            if not m or not current_file:
                continue
            line_no = int(m.group(1))
            code = m.group(2)
            for pattern, title, category, severity in _STUB_PATTERNS:
                if pattern.search(code):
                    findings.append(
                        {
                            "title": title,
                            "category": category,
                            "severity": severity,
                            "confidence": 0.8,
                            "file": current_file,
                            "start_line": line_no,
                            "end_line": line_no,
                            "description": (
                                "Flagged by the offline stub reviewer, which only "
                                "pattern-matches. Re-run without --offline for a real review."
                            ),
                            "evidence": code.strip()[:200],
                            "suggestion": "Review this line manually.",
                            "rule_ids": [],
                        }
                    )
                    break
        return LLMResponse(data={"findings": findings}, text="")

    def complete_text(self, system: str, user: str, max_tokens: int = 4000) -> LLMResponse:
        return LLMResponse(
            data={},
            text="Offline stub mode: no model was called, so there is no answer to give.",
        )

    def count_tokens(self, system: str, user: str) -> int:
        return (len(system) + len(user)) // 4  # rough, offline only


def build_llm(cfg: Config):
    return StubLLM(cfg) if cfg.offline else AnthropicLLM(cfg)
