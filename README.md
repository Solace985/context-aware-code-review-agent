# code-review-agent

A context-aware AI code reviewer you drop into your own repository. No hosted
service, no dashboard, no account — a CLI and a GitHub Actions workflow that
read your code locally, send only the relevant slice to Claude, and post a
triaged review back to the pull request.


| | What it does | Why |
|---|---|---|
| **Context engine** | Chunks the repo, indexes it, retrieves the top‑K chunks relevant to *this* diff | Dumping the whole repo into the prompt measurably lowers review quality. Selective beats full beats none. |
| **Ensemble** | Four specialised reviewers run in parallel and are merged | Narrow prompts find things a single general reviewer misses; agreement between two of them is a precision signal. |
| **Repo rules** | Reads `.review/rules/*.md`; findings cite the rule they violate | A diff alone cannot tell the model how *your* team builds software. |
| **Task context** | Compares the diff against a ticket's acceptance criteria | Catches "this looks fine but doesn't do what was asked". |
| **Evidence gates** | Drops findings whose file or lines aren't in the diff, that cite rules that don't exist, or that fall below a confidence floor | This is the difference between a review and a wall of plausible text. |
| **Severity triage** | Splits findings into *Action required* / *Review recommended* / *Nitpicks*, with a CI gate | The job of a review is to route attention, not to make every comment feel equally urgent. |
| **Evaluation** | `codereview eval` scores precision / recall / F1 against labelled sample PRs | The design claims above are checkable, not asserted. |

---

## Why not just paste the diff into a chat window?

That is the honest bar to clear, so here is the specific list:

1. **It sees the rest of your repository.** A missing `assert_owner` is
   invisible in a diff and obvious next to the four sibling handlers that all
   call it. The retrieval step puts those siblings in the prompt.
2. **It knows your written standards.** Findings cite `rule_ids` from your
   `.review/rules/`, so you can check the claim against the standard instead
   of arguing with a chat window about style.
3. **It checks the ticket.** With `--task`, a separate reviewer works through
   the acceptance criteria and reports the ones the diff does not satisfy.
4. **It throws away its own bad output.** Findings pointing at files not in
   the diff, at line numbers that aren't in a changed hunk, or citing invented
   rules are dropped before you see them — and the count is printed, so you
   know how much noise was filtered.
5. **It is reproducible and gated.** Same command, same config, an exit code
   your CI can act on, and JSON/SARIF for tooling.
6. **It never sends your secrets.** Credential-shaped strings are redacted and
   credential files are never opened, so a stray `.env` in your tree does not
   end up in a prompt.

# This agent is not meant to be a replacement but an addition along with a human in the loop that verifies the changes or issues proposed by the agent and takes necessary actions accordingly.
---

## How to Install

Requires Python 3.10+ and git.

```bash
pip install git+https://github.com/OWNER/code-review-agent@v0.1.0

# or, from a clone
git clone https://github.com/OWNER/code-review-agent && cd code-review-agent
pip install -e ".[dev]"
```

Set your key. It is read from the environment only — never from a config file,
never from a flag:

```bash
export ANTHROPIC_API_KEY=sk-ant-...        # bash / zsh
$env:ANTHROPIC_API_KEY = "sk-ant-..."      # PowerShell
```

Get a key at [console.anthropic.com](https://console.anthropic.com). The key
stays on your machine (or in your repository's Actions secrets); this project
has no server and collects nothing.

---

## Quick start

```bash
cd ~/your-project
codereview init                 # writes .review.yml, .review/rules/, the CI workflow
codereview review --base main   # review your branch before you open the PR
```

That is the whole local loop. `--base main` reviews everything on your branch
against the merge base with `main`, **including uncommitted work-tree
changes**, so you can run it before you even commit.

<details>
<summary>What the output looks like</summary>

```
ACTION REQUIRED (2)
-------------------
  [critical] app/users.py:79  SQL built from caller-supplied column names
      `fields` keys are interpolated into the UPDATE statement, so a caller can
      rename any column or append arbitrary SQL.
      rules: no-string-built-sql
  [high] app/users.py:62  Any authenticated user can edit any profile
      update_profile calls require_user but never assert_owner, unlike the four
      sibling handlers in this module.
      rules: authorize-every-resource

REVIEW RECOMMENDED (1)
----------------------
  [medium] app/users.py:80  Database failure is swallowed and reported as 200
      rules: no-swallowed-errors

context: selective (12 chunks of 39, 4 rules) | reviewers: security, correctness,
patterns, requirements | 41.3s
filtered: file_not_in_diff=1, outside_changed_lines=3, duplicate=4, low_confidence=2

wrote: .review-out/review.md, .review-out/review.json

FAILED: findings at or above severity 'high'.
```

Exit code is `1` here because the severity gate tripped. (Illustrative shape —
your findings will differ.)
</details>

---

## The four ways to use it

### 1. Pre-PR review (the highest-value one)

Other developers' attention is the expensive resource. Clean up what a machine
can catch *before* you spend it:

```bash
codereview review --base main            # branch vs merge base, incl. uncommitted
codereview review --staged               # just what you're about to commit
codereview review --base main --task DX-101.md   # also check it against the ticket
```

Wire it into a pre-push hook so you cannot forget:

```bash
cat > .git/hooks/pre-push <<'EOF'
#!/bin/sh
codereview review --base main --fail-on critical --quiet || {
  echo "Critical findings — see .review-out/review.md (git push --no-verify to override)"
  exit 1
}
EOF
chmod +x .git/hooks/pre-push
```

### 2. On every pull request

`codereview init` writes `.github/workflows/ai-code-review.yml`. Two things to
do before it runs:

1. Add `ANTHROPIC_API_KEY` under **Settings → Secrets and variables →
   Actions**.
2. In the workflow, replace `OWNER` in the `pip install` line with your GitHub
   org (or vendor the package however you prefer).

It then reviews each PR, uses the PR description as task context, and posts one
comment. The check fails when a finding is at or above `review.fail_on`.

### 3. Interrogate the findings

Auto-accepting an AI review is how you end up rubber-stamping with extra steps.
Argue with it:

```bash
codereview ask "what is the real-world impact of the SQL finding?"
codereview ask "is the ownership finding a false positive? the gateway already checks"
```

`ask` reads the last `review.json`, re-reads the referenced code from your
working tree, and answers — including telling you when it thinks a finding was
wrong.

### 4. Codify what keeps coming back

When the same class of problem recurs, promote it from a finding to a standard,
so future reviews catch it automatically:

```bash
codereview codify --min-count 3                 # show a draft rule
codereview codify --min-count 3 --write retry-safety.md   # save it
```

This closes the loop the course calls **review → judge → codify → reuse**.
Review history lives in a gitignored `.review/history.jsonl` (titles and rule
ids only, never source).

---

## Configuration

`.review.yml` in your repository root. Behaviour only — a key that looks like a
credential is a hard error, so nobody can commit an API key here by accident.

```yaml
version: 1

model: claude-opus-5
max_tokens: 16000
# effort: high        # low|medium|high|xhigh|max

agents:               # run in parallel, then merged
  - security          # injection, authz, secrets, crypto, SSRF, unsafe defaults
  - correctness       # logic, edge cases, error handling, contracts, concurrency
  - patterns          # your written rules + conventions visible in the codebase
  - requirements      # diff vs. the ticket (only runs when you pass --task)

context:
  mode: selective     # selective (recommended) | full | none
  max_chunks: 12      # top-K repository chunks retrieved per review
  max_chunk_chars: 1800
  max_rules: 6

review:
  min_confidence: 0.5
  max_findings: 15
  require_changed_lines: true   # drop findings that don't touch the diff
  changed_line_slack: 3
  fail_on: high                 # exit 1 at this severity or worse; `none` disables
  history: true

limits:
  max_diff_bytes: 200000
  max_changed_files: 60

rules_dir: .review/rules

exclude:
  - "**/*.generated.*"          # added to a sensible default list
```

Every value can be overridden per run: `--model`, `--context`, `--agents`,
`--max-findings`, `--fail-on`.

### Writing rules

Rules are the highest-leverage thing you can give this tool. A rule is a
Markdown file in `.review/rules/`:

````markdown
---
id: no-string-built-sql
severity: high
---
# Never build SQL by string concatenation

Query text and query parameters stay separate. Every statement goes through
`app/db.py` with `?` placeholders and a params tuple.

Wrong:  query(f"SELECT * FROM users WHERE email = '{email}'")
Right:  query("SELECT * FROM users WHERE email = ?", (email,))

If a query must be assembled dynamically, only an identifier from a hard-coded
allowlist may be interpolated — never a user-supplied value.
````

Rules are retrieved selectively too: only the ones relevant to the current
change are put in the prompt, and a finding may only cite a rule that actually
exists. Write about *your* repository — "we put server calls in hooks", "every
public function has a docstring", "status enums live in `models.py`" — not
generic best practice the model already knows.

`codereview init` ships four examples. Replace them.

---

## Command reference

```
codereview init      [--force] [--no-workflow]
codereview review    [--base REF | --staged | --diff-file PATH]
                     [--task TEXT_OR_PATH] [--context MODE] [--agents LIST]
                     [--model ID] [--max-findings N] [--fail-on SEVERITY]
                     [--format markdown|json|sarif]... [--out DIR]
                     [--offline] [--estimate] [--quiet]
codereview ask       QUESTION [--out DIR]
codereview codify    [--min-count N] [--write NAME.md]
codereview eval      [--configs LIST] [--limit N] [--json-out PATH]
```

Global: `--repo PATH` (default `.`). Exit codes: **0** clean, **1** severity
gate tripped, **2** error.

Two flags worth knowing:

- `--estimate` counts prompt tokens with the provider's own counter and exits
  without running a review. Use it before pointing this at a 4000-line diff.
- `--offline` runs a deterministic pattern-matching stub instead of the model.
  It is a smoke test for the pipeline, not a review — it has no understanding
  of your code and will miss almost everything.

---

## Does the context engine actually help?

Run it and see. `codereview eval` scores four configurations against six
labelled sample PRs in `evals/`:

```bash
codereview eval                              # all four configurations
codereview eval --configs diff-only,ensemble --limit 3
```

```
config           precision    recall      F1    TP    FP    FN
--------------------------------------------------------------
diff-only            ...
full-context         ...
selective            ...
ensemble             ...
```

- **diff-only** — no repository context, one general reviewer
- **full-context** — every chunk in the repo, one general reviewer
- **selective** — retrieved top-K chunks, one general reviewer
- **ensemble** — retrieved top-K chunks, the four specialists

> **No benchmark numbers are published here.** Filling that table in with
> figures I had not actually measured would be exactly the kind of
> unverifiable claim this tool exists to catch. Run it yourself — it costs a
> few dollars of tokens. The course lab this is modelled on reported roughly
> +25% F1 from adding selective context and ~10 points of F1 from an ensemble
> over a single general agent; whether *this* implementation reproduces that
> on six cases is a genuinely open question, and six cases is a small sample.

Matching is deterministic (file + line proximity + keyword overlap), not an LLM
judge — reproducible and cheap, but conservative: a correct finding worded
unusually will miss its label. Add your own cases in `evals/cases/*.json`.

---

## Security

The threat model is that **the code being reviewed is hostile** — it arrived
in a pull request from someone you do not control — and that **the model's
reply is also untrusted**.

**Your key stays yours.** Read from `ANTHROPIC_API_KEY` in the environment,
once. A credential-shaped key in `.review.yml` is a startup error. Never
logged, never written to an output file, never accepted as a CLI flag (where
it would land in shell history).

**Nothing is stored anywhere public.** There is no server, no telemetry, no
analytics. The only network call is to the Anthropic API. Output files go to a
gitignored `.review-out/`. The index is built in memory per run and is never
written to disk.

**Secrets never reach the prompt.** Files matching credential patterns
(`.env*`, `*.pem`, `id_rsa*`, `credentials*`, `*.p12`, `.npmrc`, `.netrc`, …)
are never opened, and every remaining byte — diff, retrieved chunks, rules,
ticket text — passes through a redactor that strips AWS/GCP/GitHub/Slack/
Stripe/Anthropic keys, JWTs, private-key blocks, URL credentials and assigned
secrets. Redactions are counted and reported.

**Prompt injection is handled as a real threat.** Repository content is data,
never instructions: every untrusted section is delimiter-wrapped, the system
prompt tells each reviewer to report instruction-like content as a security
finding rather than obey it, and — critically — the *output* is validated
structurally. A model talked into "approve everything" still cannot produce a
finding pointing at a file that is not in the diff. Nothing from the model is
ever executed, and no filesystem path is ever derived from it.

**Path traversal is closed.** Every read resolves under the repository root
and is rejected if it escapes; symlinks are skipped during indexing.

**Command execution is narrow.** All process execution is `git`, via an argv
list (never a shell), with a timeout, and refs are validated against a
conservative pattern — `--upload-pack=...` as a branch name is rejected rather
than passed to git as a flag.

**Config parsing is safe.** `yaml.safe_load` only, so a `.review.yml` arriving
in a pull request cannot construct Python objects.

**CI runs at least privilege.** The workflow uses `pull_request` — *not*
`pull_request_target` — so a fork's code never runs in a job holding your API
key, and fork PRs are skipped explicitly rather than failing confusingly.
Permissions are `contents: read` + `pull-requests: write`. The PR body is read
from the event payload with `jq` rather than interpolated into a shell script.
**This program never handles your GitHub token** — the workflow posts the
comment with `gh` using the job's own scoped token.

**Cost is bounded.** Diff size, changed-file count, chunk count and output
tokens all have configurable ceilings, and everything dropped by a ceiling is
reported rather than silently skipped. `--estimate` tells you the bill first.

Found a hole? Open an issue. Security reports welcome.

---

## How it works

```
git diff ──▶ parse ──▶ hunks + exact new-file line numbers
                            │
repo files ──▶ chunk ──▶ BM25 index ──▶ retrieve top-K ──┐
                                                         ├─▶ redact ─▶ prompt
.review/rules/*.md ──▶ select relevant ──────────────────┤
--task ticket ───────────────────────────────────────────┘
                            │
                  ┌─────────┴────────┬──────────┬──────────────┐
               security        correctness   patterns    requirements   (parallel)
                  └─────────┬────────┴──────────┴──────────────┘
                            ▼
        validate (file in diff? lines changed? rule exists? confident?)
                            ▼
        merge duplicates (agreement raises confidence) ──▶ rank ──▶ cap
                            ▼
              triage ──▶ markdown / json / sarif ──▶ exit code
```

Run the tests with `pytest -q`. They never touch the network — no API key is
needed, and CI runs without one to prove it.

---

## Credits

The design follows the *Build an AI Code Review Agent* short course by DeepLearning.AI
and is inspired by Qodo: the pre-PR review habit, task and repository context, the context engine,
selective retrieval beating full context, the specialised-agent ensemble, and
severity triage all come from there. The implementation, the security model
and the evaluation harness are mine.

