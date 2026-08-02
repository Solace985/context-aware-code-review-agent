"""Tests for the provider layer.

There is no API key in CI, so the SDK client is replaced with a fake. That
still exercises everything this module owns: key handling, request shape,
response parsing, and each documented failure mode.
"""

import json
from types import SimpleNamespace

import anthropic
import pytest

from codereview.agents import FINDINGS_SCHEMA
from codereview.config import Config
from codereview.llm import AnthropicLLM, StubLLM, _extract_json, build_llm
from codereview.models import ReviewError


def make_response(text="{}", stop_reason="end_turn", **extra):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=123, output_tokens=45),
        **extra,
    )


class FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def count_tokens(self, **kwargs):
        return SimpleNamespace(input_tokens=999)


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


@pytest.fixture
def llm_factory(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    created: dict = {}

    def build(responses, **cfg_kwargs):
        def fake_ctor(**kwargs):
            created.update(kwargs)
            return FakeClient(responses)

        monkeypatch.setattr(anthropic, "Anthropic", fake_ctor)
        llm = AnthropicLLM(Config(**cfg_kwargs))
        llm.created_with = created
        return llm

    return build


# --- key handling ---------------------------------------------------------


def test_missing_key_is_a_clear_actionable_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ReviewError) as exc:
        AnthropicLLM(Config())
    assert "ANTHROPIC_API_KEY is not set" in str(exc.value)
    assert "--offline" in str(exc.value)


def test_blank_key_is_treated_as_missing(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    with pytest.raises(ReviewError, match="is not set"):
        AnthropicLLM(Config())


def test_key_and_limits_are_passed_to_the_client(llm_factory):
    llm = llm_factory([make_response()], timeout_s=42.0, max_retries=5)
    assert llm.created_with["api_key"] == "sk-ant-test-key"
    assert llm.created_with["timeout"] == 42.0
    assert llm.created_with["max_retries"] == 5


def test_the_key_never_appears_in_an_error_message(llm_factory):
    llm = llm_factory([anthropic.APIConnectionError(request=None)])
    with pytest.raises(ReviewError) as exc:
        llm.complete_json("sys", "user", FINDINGS_SCHEMA)
    assert "sk-ant-test-key" not in str(exc.value)


# --- request shape --------------------------------------------------------


def test_structured_output_is_requested_with_the_schema(llm_factory):
    llm = llm_factory([make_response('{"findings": []}')])
    llm.complete_json("system text", "user text", FINDINGS_SCHEMA)
    sent = llm.client.messages.calls[0]
    assert sent["model"] == "claude-opus-5"
    assert sent["system"] == "system text"
    assert sent["messages"] == [{"role": "user", "content": "user text"}]
    assert sent["output_config"]["format"] == {"type": "json_schema", "schema": FINDINGS_SCHEMA}
    assert "effort" not in sent["output_config"]  # unset by default


def test_effort_is_sent_only_when_configured(llm_factory):
    llm = llm_factory([make_response('{"findings": []}')], effort="high")
    llm.complete_json("s", "u", FINDINGS_SCHEMA)
    assert llm.client.messages.calls[0]["output_config"]["effort"] == "high"


def test_plain_text_calls_send_no_schema(llm_factory):
    llm = llm_factory([make_response("hello")])
    reply = llm.complete_text("s", "u", max_tokens=100)
    assert reply.text == "hello"
    sent = llm.client.messages.calls[0]
    assert "output_config" not in sent
    assert sent["max_tokens"] == 100


# --- response handling ----------------------------------------------------


def test_usage_is_recorded(llm_factory):
    llm = llm_factory([make_response('{"findings": []}')])
    reply = llm.complete_json("s", "u", FINDINGS_SCHEMA)
    assert (reply.input_tokens, reply.output_tokens) == (123, 45)
    assert reply.data == {"findings": []}


def test_a_refusal_is_reported_not_parsed(llm_factory):
    llm = llm_factory(
        [
            SimpleNamespace(
                content=[],
                stop_reason="refusal",
                stop_details=SimpleNamespace(category="cyber", explanation="no"),
                usage=SimpleNamespace(input_tokens=1, output_tokens=0),
            )
        ]
    )
    with pytest.raises(ReviewError, match="declined this request"):
        llm.complete_json("s", "u", FINDINGS_SCHEMA)


def test_truncated_json_says_how_to_fix_it(llm_factory):
    llm = llm_factory([make_response('{"findings": [{"title": "half', stop_reason="max_tokens")])
    with pytest.raises(ReviewError, match="max_tokens"):
        llm.complete_json("s", "u", FINDINGS_SCHEMA)


def test_truncated_plain_text_is_returned_rather_than_raising(llm_factory):
    llm = llm_factory([make_response("a long answer cut off", stop_reason="max_tokens")])
    assert llm.complete_text("s", "u").text.startswith("a long answer")


# --- failure modes --------------------------------------------------------


@pytest.mark.parametrize(
    "error,expected",
    [
        (anthropic.AuthenticationError, "rejected by the API"),
        (anthropic.PermissionDeniedError, "lacks access"),
        (anthropic.NotFoundError, "not found"),
        (anthropic.RateLimitError, "rate limited"),
    ],
)
def test_api_errors_become_actionable_messages(llm_factory, error, expected):
    response = SimpleNamespace(status_code=400, headers={}, request=None)
    llm = llm_factory([error("boom", response=response, body=None)])
    with pytest.raises(ReviewError, match=expected):
        llm.complete_json("s", "u", FINDINGS_SCHEMA)


def test_a_model_without_structured_outputs_falls_back_to_prose_json(llm_factory):
    """An older model 400s on output_config; the review must still complete."""
    response = SimpleNamespace(status_code=400, headers={}, request=None)
    rejection = anthropic.BadRequestError(
        "output_config: unsupported for this model", response=response, body=None
    )
    llm = llm_factory([rejection, make_response('{"findings": []}')])
    reply = llm.complete_json("system", "user", FINDINGS_SCHEMA)
    assert reply.data == {"findings": []}

    first, second = llm.client.messages.calls
    assert "output_config" in first
    assert "output_config" not in second
    assert "OUTPUT FORMAT" in second["system"]
    assert "findings" in second["system"]  # the schema is described in prose

    # The fallback is remembered, so the next call does not re-trigger a 400.
    llm.client.messages._responses.append(make_response('{"findings": []}'))
    llm.complete_json("system", "user", FINDINGS_SCHEMA)
    assert "output_config" not in llm.client.messages.calls[-1]


def test_other_400s_are_not_silently_retried(llm_factory):
    response = SimpleNamespace(status_code=400, headers={}, request=None)
    llm = llm_factory(
        [anthropic.BadRequestError("max_tokens must be positive", response=response, body=None)]
    )
    with pytest.raises(ReviewError, match="400"):
        llm.complete_json("s", "u", FINDINGS_SCHEMA)
    assert len(llm.client.messages.calls) == 1


def test_output_config_moves_to_extra_body_on_an_older_sdk(llm_factory):
    llm = llm_factory([make_response('{"findings": []}')])
    real_create = llm.client.messages.create
    state = {"first": True}

    def create(**kwargs):
        if state["first"] and "output_config" in kwargs:
            state["first"] = False
            raise TypeError("unexpected keyword argument 'output_config'")
        return real_create(**kwargs)

    llm.client.messages.create = create
    llm.complete_json("s", "u", FINDINGS_SCHEMA)
    assert "output_config" in llm.client.messages.calls[-1]["extra_body"]


def test_count_tokens_uses_the_provider(llm_factory):
    llm = llm_factory([])
    assert llm.count_tokens("s", "u") == 999


# --- JSON extraction ------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '{"findings": []}',
        '```json\n{"findings": []}\n```',
        '```\n{"findings": []}\n```',
        'Here you go:\n{"findings": []}\nHope that helps.',
    ],
)
def test_json_is_extracted_from_messy_replies(text):
    assert _extract_json(text) == {"findings": []}


def test_nested_braces_and_strings_do_not_confuse_the_extractor():
    payload = {"findings": [{"title": 'a }{ brace "quoted"', "nested": {"x": 1}}]}
    text = "prose before " + json.dumps(payload) + " prose after"
    assert _extract_json(text) == payload


@pytest.mark.parametrize("text", ["no json here", '{"unterminated": ', "[1, 2, 3]"])
def test_unparseable_replies_raise(text):
    with pytest.raises(ReviewError):
        _extract_json(text)


# --- offline stub ---------------------------------------------------------


def test_build_llm_selects_the_stub_when_offline():
    assert isinstance(build_llm(Config(offline=True)), StubLLM)


def test_stub_needs_no_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    stub = StubLLM(Config(offline=True))
    reply = stub.complete_json("s", "### FILE: a.py (modified)\n    12 +     eval(x)\n", {})
    assert reply.data["findings"][0]["file"] == "a.py"
    assert reply.data["findings"][0]["start_line"] == 12
