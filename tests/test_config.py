import pytest

from codereview.config import CONFIG_FILENAME, load_config
from codereview.models import ReviewError


def write_cfg(tmp_path, body: str):
    (tmp_path / CONFIG_FILENAME).write_text(body, encoding="utf-8")
    return tmp_path


def test_defaults_when_no_config_file(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg.model == "claude-opus-5"
    assert cfg.context_mode == "selective"
    assert cfg.fail_on == "high"
    assert "security" in cfg.agents


def test_reads_values_from_file(tmp_path):
    write_cfg(tmp_path, "model: claude-sonnet-5\nreview:\n  fail_on: critical\n  max_findings: 3\n")
    cfg = load_config(tmp_path)
    assert cfg.model == "claude-sonnet-5"
    assert cfg.fail_on == "critical"
    assert cfg.max_findings == 3


@pytest.mark.parametrize(
    "body",
    [
        "api_key: sk-ant-secret\n",
        "anthropic_token: abc\n",
        "provider:\n  secret: abc\n",
        "PASSWORD: hunter2\n",
    ],
)
def test_credentials_in_config_are_a_hard_error(tmp_path, body):
    write_cfg(tmp_path, body)
    with pytest.raises(ReviewError, match="not allowed"):
        load_config(tmp_path)


def test_legitimate_keys_containing_a_forbidden_word_still_load(tmp_path):
    # `max_tokens` contains "token" — the credential guard must not eat it.
    write_cfg(tmp_path, "max_tokens: 8000\n")
    assert load_config(tmp_path).max_tokens == 8000


def test_the_shipped_default_config_loads(tmp_path):
    from codereview.config import DEFAULT_CONFIG_YAML

    write_cfg(tmp_path, DEFAULT_CONFIG_YAML)
    cfg = load_config(tmp_path)
    assert cfg.model == "claude-opus-5"
    assert cfg.max_tokens == 16000


def test_unknown_agent_is_rejected(tmp_path):
    write_cfg(tmp_path, "agents: [security, sorcery]\n")
    with pytest.raises(ReviewError, match="unknown agent"):
        load_config(tmp_path)


def test_invalid_enum_values_are_rejected(tmp_path):
    write_cfg(tmp_path, "context:\n  mode: telepathy\n")
    with pytest.raises(ReviewError, match="context.mode"):
        load_config(tmp_path)


def test_numeric_values_are_clamped_not_trusted(tmp_path):
    write_cfg(tmp_path, "review:\n  min_confidence: 9.5\ncontext:\n  max_chunks: 99999\n")
    cfg = load_config(tmp_path)
    assert cfg.min_confidence == 1.0
    assert cfg.max_chunks == 100


def test_rules_dir_cannot_escape_the_repo(tmp_path):
    write_cfg(tmp_path, "rules_dir: ../../etc\n")
    with pytest.raises(ReviewError, match="inside the repository"):
        load_config(tmp_path)


def test_malformed_yaml_reports_clearly(tmp_path):
    write_cfg(tmp_path, "model: [unclosed\n")
    with pytest.raises(ReviewError, match="could not be parsed"):
        load_config(tmp_path)


def test_yaml_is_loaded_safely(tmp_path):
    # yaml.safe_load must refuse to construct arbitrary Python objects.
    write_cfg(tmp_path, "model: !!python/object/apply:os.system ['echo pwned']\n")
    with pytest.raises(ReviewError):
        load_config(tmp_path)


def test_user_excludes_extend_rather_than_replace_defaults(tmp_path):
    write_cfg(tmp_path, 'exclude: ["**/*.generated.py"]\n')
    cfg = load_config(tmp_path)
    assert "**/node_modules/**" in cfg.exclude
    assert "**/*.generated.py" in cfg.exclude


def test_cli_overrides_win(tmp_path):
    write_cfg(tmp_path, "model: claude-sonnet-5\n")
    cfg = load_config(tmp_path, {"model": "claude-opus-5", "offline": True})
    assert cfg.model == "claude-opus-5"
    assert cfg.offline is True
