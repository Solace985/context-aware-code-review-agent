import pytest

from codereview.diff import (
    apply_limits,
    diff_query_terms,
    line_is_changed,
    parse_diff,
    resolve_base,
)
from codereview.models import ReviewError

SAMPLE = """\
diff --git a/app/users.py b/app/users.py
index 1111111..2222222 100644
--- a/app/users.py
+++ b/app/users.py
@@ -10,6 +10,9 @@ def existing():
     keep_this()
     also_this()
-    removed_call()
+    added_call()
+    another_added()
     trailing()
diff --git a/README.md b/README.md
new file mode 100644
--- /dev/null
+++ b/README.md
@@ -0,0 +1,2 @@
+# Title
+body
diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-gone()
-also_gone()
"""


def test_parses_all_files_and_statuses():
    diff = parse_diff(SAMPLE)
    assert diff.paths == ["app/users.py", "README.md", "old.py"]
    assert [f.status for f in diff.files] == ["modified", "added", "deleted"]


def test_added_line_numbers_are_absolute_in_the_new_file():
    diff = parse_diff(SAMPLE)
    users = diff.by_path("app/users.py")
    assert users is not None
    # hunk starts at new line 10: two context lines, then the additions.
    assert users.added_line_numbers == {12, 13}
    assert users.hunks[0].added == [(12, "    added_call()"), (13, "    another_added()")]
    assert users.hunks[0].removed == ["    removed_call()"]


def test_ordered_lines_keep_interleaving_for_rendering():
    diff = parse_diff(SAMPLE)
    kinds = [k for k, _, _ in diff.by_path("app/users.py").hunks[0].lines]
    assert kinds == [" ", " ", "-", "+", "+", " "]


def test_rename_is_detected():
    text = (
        "diff --git a/a/old.py b/a/new.py\n"
        "similarity index 90%\n"
        "rename from a/old.py\n"
        "rename to a/new.py\n"
        "--- a/a/old.py\n"
        "+++ b/a/new.py\n"
        "@@ -1,2 +1,2 @@\n"
        " same\n"
        "+changed\n"
    )
    diff = parse_diff(text)
    assert diff.files[0].status == "renamed"
    assert diff.files[0].path == "a/new.py"
    assert diff.files[0].old_path == "a/old.py"


def test_binary_file_marked():
    text = (
        "diff --git a/logo.png b/logo.png\n"
        "index 0000000..1111111 100644\n"
        "Binary files a/logo.png and b/logo.png differ\n"
    )
    diff = parse_diff(text)
    assert diff.files[0].binary is True


def test_line_is_changed_respects_slack():
    diff = parse_diff(SAMPLE)
    users = diff.by_path("app/users.py")
    assert line_is_changed(users, 12, 12, slack=0)
    assert line_is_changed(users, 15, 15, slack=3)
    assert not line_is_changed(users, 40, 41, slack=3)


def test_deleted_file_is_never_rejected_on_line_numbers():
    diff = parse_diff(SAMPLE)
    assert line_is_changed(diff.by_path("old.py"), 999, 999, slack=0)


def test_query_terms_prefer_identifiers_from_the_change():
    terms = diff_query_terms(parse_diff(SAMPLE))
    assert "users" in terms  # from the path
    assert "added_call" in terms
    assert "return" not in terms  # stopword


def test_apply_limits_records_what_it_dropped():
    diff = apply_limits(parse_diff(SAMPLE), max_files=1, max_bytes=1_000_000)
    assert len(diff.files) == 1
    assert diff.truncated is True
    assert len(diff.dropped_files) == 2


def test_apply_limits_drops_whole_files_to_fit_the_byte_budget():
    diff = apply_limits(parse_diff(SAMPLE), max_files=50, max_bytes=300)
    assert diff.truncated is True
    assert diff.dropped_files  # named, not silently cut mid-hunk
    # What survives in `files` is exactly what `raw` reports, so the prompt
    # rendered from `files` cannot exceed what the limit claims to bound.
    assert diff.raw == "\n".join(f.raw for f in diff.files)
    assert len(diff.raw.encode()) <= 300


def test_apply_limits_always_keeps_at_least_one_file():
    diff = apply_limits(parse_diff(SAMPLE), max_files=50, max_bytes=1)
    assert len(diff.files) == 1


def test_apply_limits_flags_oversized_hunks():
    from codereview.diff import MAX_HUNK_LINES

    body = "\n".join(f"+line {i}" for i in range(MAX_HUNK_LINES + 20))
    text = (
        "diff --git a/big.py b/big.py\n--- a/big.py\n+++ b/big.py\n"
        f"@@ -1,1 +1,{MAX_HUNK_LINES + 20} @@\n{body}\n"
    )
    diff = apply_limits(parse_diff(text), max_files=50, max_bytes=1_000_000)
    assert diff.truncated_hunks == 1


def test_apply_limits_leaves_a_small_diff_alone():
    diff = apply_limits(parse_diff(SAMPLE), max_files=50, max_bytes=1_000_000)
    assert diff.truncated is False
    assert diff.dropped_files == []


@pytest.mark.parametrize("ref", ["--upload-pack=evil", "-x", "a b", "$(whoami)", "a;b"])
def test_unsafe_refs_are_rejected(tmp_path, ref):
    with pytest.raises(ReviewError, match="unsafe git ref"):
        resolve_base(tmp_path, ref)


def test_empty_diff_parses_to_nothing():
    assert parse_diff("").files == []


def test_content_lines_that_look_like_file_headers_are_not_headers():
    # Removing YAML front matter produces a body line of "----", and adding
    # a line of plus signs produces "+++ ...". Neither starts a new file.
    text = (
        "diff --git a/doc.md b/doc.md\n"
        "--- a/doc.md\n"
        "+++ b/doc.md\n"
        "@@ -1,4 +1,4 @@\n"
        "---- \n"
        "-title: old\n"
        "+++ new marker\n"
        "+title: new\n"
        " body\n"
    )
    diff = parse_diff(text)
    assert diff.paths == ["doc.md"]
    hunk = diff.files[0].hunks[0]
    assert hunk.removed == ["--- ", "title: old"]
    assert [t for _, t in hunk.added] == ["++ new marker", "title: new"]
    assert diff.files[0].added_line_numbers == {1, 2}


def test_a_second_file_after_a_hunk_still_starts_a_new_file():
    text = (
        "diff --git a/one.py b/one.py\n--- a/one.py\n+++ b/one.py\n"
        "@@ -1 +1,2 @@\n+first\n"
        "diff --git a/two.py b/two.py\n--- a/two.py\n+++ b/two.py\n"
        "@@ -1 +1,2 @@\n+second\n"
    )
    diff = parse_diff(text)
    assert diff.paths == ["one.py", "two.py"]
    assert [t for _, t in diff.files[1].hunks[0].added] == ["second"]
