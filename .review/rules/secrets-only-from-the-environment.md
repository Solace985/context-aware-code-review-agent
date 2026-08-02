---
id: secrets-only-from-the-environment
severity: critical
---
# Credentials come from the environment and go nowhere else

`ANTHROPIC_API_KEY` is read exactly once, in `llm.AnthropicLLM.__init__`, from
`os.environ`. It must never be:

- accepted from `.review.yml` (`config._check_no_secrets` enforces this — a
  new legitimate key whose name contains `token`/`auth`/`secret` must be added
  to `_ALLOWED_KEYS`, not exempted from the check);
- accepted from a CLI flag, where it would land in shell history and `ps`;
- interpolated into an error message, a log line, or any output file.

Anything bound for the model goes through `safety.redact` first, and any file
whose name matches `safety.SENSITIVE_PATH_PATTERNS` is never opened at all.
A new outbound payload that skips redaction is a defect even when the content
looks harmless.

The GitHub token is deliberately outside this program: the workflow posts the
comment with `gh`, so this code never holds it.
