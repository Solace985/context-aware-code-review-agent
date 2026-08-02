from codereview.config import Config, load_config
from codereview.context import (
    BM25Index,
    build_index,
    chunk_file,
    excluded,
    load_rules,
    retrieve,
    select_rules,
    tokenize,
)
from codereview.diff import parse_diff

PY_SOURCE = '''\
"""Module docstring."""
import os

CONSTANT = 1


def compute_invoice_total(items):
    return sum(i.price for i in items)


class InvoiceService:
    def __init__(self, repo):
        self.repo = repo

    def issue_invoice(self, customer_id):
        return self.repo.save(customer_id)
'''

TS_SOURCE = """\
import { db } from './db';

export function getUser(id: number) {
  return db.query('select * from users where id = ?', [id]);
}

export class UserService {
  list() {
    return db.query('select * from users');
  }
}
"""


def test_tokenizer_splits_camel_and_snake_case():
    tokens = tokenize("getUserById  compute_invoice_total")
    assert "getuserbyid" in tokens
    assert "user" in tokens and "get" in tokens
    assert "invoice" in tokens and "total" in tokens


def test_python_chunking_uses_the_ast():
    chunks = chunk_file("app/invoice.py", PY_SOURCE)
    names = {c.name for c in chunks}
    assert "compute_invoice_total" in names
    assert "InvoiceService" in names
    assert "InvoiceService.issue_invoice" in names
    fn = next(c for c in chunks if c.name == "compute_invoice_total")
    assert fn.kind == "function"
    assert "return sum" in fn.text
    assert fn.start_line < fn.end_line


def test_python_chunking_keeps_module_level_code_reachable():
    chunks = chunk_file("app/invoice.py", PY_SOURCE)
    module = next(c for c in chunks if c.kind == "module")
    assert "CONSTANT = 1" in module.text


def test_unparseable_python_falls_back_to_windows():
    chunks = chunk_file("broken.py", "def oops(:\n  ???\n")
    assert chunks and all(c.kind == "window" for c in chunks)


def test_declaration_chunking_for_typescript():
    chunks = chunk_file("src/users.ts", TS_SOURCE)
    names = {c.name for c in chunks}
    assert "getUser" in names
    assert "UserService" in names


def test_unknown_extension_uses_windows():
    chunks = chunk_file("notes.md", "line\n" * 200)
    assert len(chunks) > 1
    assert all(c.kind == "window" for c in chunks)


def test_exclude_globs_match_nested_and_top_level():
    patterns = ["**/node_modules/**", "**/*.min.js"]
    assert excluded("node_modules/x/index.js", patterns)
    assert excluded("web/node_modules/x/index.js", patterns)
    assert excluded("static/app.min.js", patterns)
    assert not excluded("src/app.js", patterns)


def test_bm25_ranks_the_relevant_chunk_first():
    chunks = chunk_file("app/invoice.py", PY_SOURCE) + chunk_file("src/users.ts", TS_SOURCE)
    index = BM25Index(chunks)
    scores = index.score(tokenize("invoice total"))
    best = chunks[max(range(len(chunks)), key=lambda i: scores[i])]
    assert "invoice" in best.name.lower()


def _repo(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "invoice.py").write_text(PY_SOURCE, encoding="utf-8")
    (tmp_path / "app" / "unrelated.py").write_text("def zzz_nothing():\n    return 0\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=abc123456789", encoding="utf-8")
    return load_config(tmp_path)


def test_index_skips_credential_files(tmp_path):
    cfg = _repo(tmp_path)
    index, files, skipped, hit_cap = build_index(cfg)
    assert ".env" in skipped
    assert ".env" not in files
    assert index.chunks
    assert hit_cap is False


def test_index_reports_when_the_file_cap_is_reached(tmp_path):
    cfg = _repo(tmp_path)
    cfg.index_max_files = 1
    _, files, _, hit_cap = build_index(cfg)
    assert len(files) == 1
    assert hit_cap is True


def test_index_prunes_vendored_directories(tmp_path):
    cfg = _repo(tmp_path)
    vendored = tmp_path / "node_modules" / "pkg"
    vendored.mkdir(parents=True)
    (vendored / "index.js").write_text("export function x() {}\n", encoding="utf-8")
    _, files, _, _ = build_index(cfg)
    assert not any("node_modules" in f for f in files)


def test_retrieval_selects_the_related_chunk(tmp_path):
    cfg = _repo(tmp_path)
    index, *_ = build_index(cfg)
    diff = parse_diff(
        "diff --git a/app/billing.py b/app/billing.py\n"
        "--- a/app/billing.py\n"
        "+++ b/app/billing.py\n"
        "@@ -1,2 +1,3 @@\n"
        " x = 1\n"
        "+total = compute_invoice_total(items)\n"
    )
    chunks = retrieve(index, tokenize("compute_invoice_total invoice"), diff, top_k=3, max_chunk_chars=2000)
    assert chunks
    assert any("compute_invoice_total" in c.text for c in chunks)


def test_retrieval_truncates_oversized_chunks(tmp_path):
    cfg = _repo(tmp_path)
    index, *_ = build_index(cfg)
    diff = parse_diff("diff --git a/app/x.py b/app/x.py\n--- a/app/x.py\n+++ b/app/x.py\n@@ -1 +1,2 @@\n+invoice\n")
    chunks = retrieve(index, tokenize("invoice"), diff, top_k=3, max_chunk_chars=40)
    assert all(len(c.text) <= 100 for c in chunks)
    assert any("chunk truncated" in c.text for c in chunks)


def test_rules_are_loaded_with_frontmatter(tmp_path):
    rules_dir = tmp_path / ".review" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "no-sql.md").write_text(
        "---\nid: no-raw-sql\nseverity: high\n---\n# Never build SQL by hand\n\nbody\n",
        encoding="utf-8",
    )
    (rules_dir / "plain.md").write_text("# Plain rule\n\nbody\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    rules = load_rules(cfg)
    ids = {r.id for r in rules}
    assert ids == {"no-raw-sql", "plain"}
    sql_rule = next(r for r in rules if r.id == "no-raw-sql")
    assert sql_rule.severity == "high"
    assert sql_rule.title == "Never build SQL by hand"
    assert "---" not in sql_rule.text


def test_select_rules_returns_everything_below_the_limit():
    from codereview.models import Rule

    rules = [Rule(f"r{i}", f"Rule {i}", "p.md", "low", "text") for i in range(3)]
    assert select_rules(rules, ["anything"], parse_diff(""), limit=6) == rules


def test_select_rules_picks_the_relevant_ones():
    from codereview.models import Rule

    rules = [
        Rule("sql", "SQL", "a.md", "high", "never interpolate sql query strings"),
        Rule("naming", "Naming", "b.md", "low", "use snake case for module names"),
        Rule("tests", "Tests", "c.md", "low", "every module needs a unit test file"),
    ]
    picked = select_rules(rules, tokenize("sql query interpolate"), parse_diff(""), limit=1)
    assert [r.id for r in picked] == ["sql"]


def test_load_task_from_file_and_inline(tmp_path):
    from codereview.context import load_task

    cfg = Config(root=tmp_path)
    (tmp_path / "ticket.md").write_text("Acceptance criteria: do the thing", encoding="utf-8")
    text, source = load_task(cfg, "ticket.md")
    assert "Acceptance criteria" in text
    assert source == "ticket.md"

    text, source = load_task(cfg, "just some inline text")
    assert text == "just some inline text"
    assert source == "inline"

    assert load_task(cfg, None) == ("", "")
