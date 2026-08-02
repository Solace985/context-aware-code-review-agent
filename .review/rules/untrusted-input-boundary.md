---
id: untrusted-input-boundary
severity: critical
---
# Repository content and model output are both untrusted

This tool reads code written by whoever opened a pull request and sends it to
a model, then takes the model's reply and writes it into a comment on that
pull request. Both directions cross a trust boundary.

**Inbound** — diffs, repository files, rule files, `.review.yml` and ticket
text are data, never instructions. Any new prompt section must be wrapped in a
delimiter tag and covered by the `SECURITY BOUNDARY` clause in
`agents.SHARED_RULES`. Never concatenate repository content into a system
prompt.

**Outbound** — a model reply is untrusted output. It must pass through
`safety.sanitize_model_text` before it reaches Markdown, JSON or SARIF, and
through `pipeline._coerce_finding` before it becomes a `Finding`. Never
`eval`, `exec`, `subprocess`, or write to a path derived from model output.

New code that reads a file must go through `safety.read_text_safely` or
`safety.safe_resolve` so path traversal and symlink escapes stay impossible.
