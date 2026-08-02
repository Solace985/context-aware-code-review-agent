---
id: subprocess-argv-only
severity: high
---
# Shell out through argv, with a timeout, and validate every ref

All process execution lives in `diff._run_git`. Any new call must keep its
three properties:

1. **argv list, never `shell=True`.** A branch called `$(rm -rf ~)` is a
   perfectly legal git ref name.
2. **An explicit `timeout`.** A hung `git` must not hang the review.
3. **Validated refs.** User-supplied refs go through `diff._validate_ref`
   first. A ref beginning with `-` is read by git as a flag
   (`--upload-pack=...` is the classic exploit), which is why the pattern
   requires an alphanumeric first character, and why callers pass `--`.

Return codes are checked and turned into a `ReviewError` with the first line
of stderr. Do not let a non-zero exit fall through as empty output.
