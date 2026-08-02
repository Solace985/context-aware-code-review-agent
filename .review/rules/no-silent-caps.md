---
id: no-silent-caps
severity: high
---
# Every cap, filter and truncation must be reported

A reviewer that quietly skips half the diff is worse than no reviewer, because
the clean result reads as coverage. Any code path that bounds what gets
reviewed must record the fact:

- dropped or truncated diff content sets `Diff.truncated` / `dropped_files`
  and appends to `ReviewResult.warnings`;
- a discarded finding increments a named counter in the `dropped` dict, which
  is surfaced in both the Markdown and the JSON report;
- a reviewer that fails degrades to a warning, and only a total failure raises.

Adding a new limit means adding its counter and its line in the report in the
same change. Silently `break`ing out of a loop over changed files, or
`continue`ing past a finding, is the specific thing this rule prohibits.
