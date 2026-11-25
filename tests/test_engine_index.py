import json

import pytest

from conftest import commit_all, git, write_task
from engine import Engine, EngineError, fold, normalize_tags
from graph import cycle_after_setting, find_cycle


def test_non_git_target_rejected(tmp_path):
    (tmp_path / "plain").mkdir()
    with pytest.raises(EngineError):
        Engine(tmp_path / "plain", db_path=tmp_path / "db.sqlite3")


def test_full_hydration_then_cache_hit(repo, make_engine):
    write_task(repo, "TKT-1", title="תיקון באג", tags=["Backend", "דחוף"])
    write_task(repo, "TKT-2", blocked_by=["TKT-1"])
    commit_all(repo)
    eng = make_engine()
    s = eng.refresh()
    assert s["full"] and s["tasks_added"] == 2
    assert eng.get_task("TKT-1")["title"] == "תיקון באג"
    assert eng.get_task("TKT-2")["blocked_by"] == ["TKT-1"]
    assert eng.get_task("TKT-1")["blocks"] == ["TKT-2"]
    # second engine (new boot) -> cache hit, nothing re-parsed
    assert not make_engine().refresh().changed


def test_differential_hydration_add_modify_delete(repo, make_engine):
    write_task(repo, "TKT-1")
    write_task(repo, "TKT-2")
    commit_all(repo)
    eng = make_engine()
    eng.refresh()
    write_task(repo, "TKT-1", title="changed")
    (repo / ".todo/tasks/TKT-2/main.json5").unlink()
    write_task(repo, "TKT-3")
    commit_all(repo)
    s = eng.refresh()
    assert not s["full"]
    assert (s["tasks_added"], s["tasks_updated"], s["tasks_deleted"]) == (1, 1, 1)
    assert [t["id"] for t in eng.list_tasks()] == ["TKT-1", "TKT-3"]
    assert eng.get_task("TKT-1")["title"] == "changed"


def test_rename_handled(repo, make_engine):
    write_task(repo, "OLD")
    commit_all(repo)
    eng = make_engine()
    eng.refresh()
    (repo / ".todo/tasks/NEW").mkdir()
    git(repo, "mv", ".todo/tasks/OLD/main.json5", ".todo/tasks/NEW/main.json5")
    (repo / ".todo/tasks/NEW/main.json5").write_text(json.dumps({"id": "NEW", "title": "x"}))
    commit_all(repo)
    eng.refresh()
    assert [t["id"] for t in eng.list_tasks()] == ["NEW"]


def test_uncommitted_edit_detected_and_reverted(repo, make_engine):
    write_task(repo, "TKT-1", title="a")
    commit_all(repo)
    eng = make_engine()
    eng.refresh()
    write_task(repo, "TKT-1", title="dirty")
    assert eng.refresh().changed
    assert eng.get_task("TKT-1")["title"] == "dirty"
    git(repo, "checkout", "--", ".todo")
    eng.refresh()
    assert eng.get_task("TKT-1")["title"] == "a"


def test_unreachable_old_commit_falls_back_to_full(repo, make_engine):
    write_task(repo, "TKT-1")
    commit_all(repo)
    eng = make_engine()
    eng.refresh()
    eng.db.execute("UPDATE projects SET last_scanned_commit=?", ("0" * 40,))
    assert eng.refresh()["full"]


def test_empty_repo_without_commits(repo, make_engine):
    write_task(repo, "TKT-1")
    eng = make_engine()
    eng.refresh()
    assert len(eng.list_tasks()) == 1


def test_bad_file_skipped_with_warning(repo, make_engine):
    write_task(repo, "TKT-1")
    (repo / ".todo/tasks/BAD").mkdir(parents=True)
    (repo / ".todo/tasks/BAD/main.json5").write_text("{ nope")
    commit_all(repo)
    eng = make_engine()
    eng.refresh()
    assert [t["id"] for t in eng.list_tasks()] == ["TKT-1"]
    assert any("BAD" in w for w in eng.warnings)


def test_tag_normalisation():
    assert normalize_tags([" Backend ", "backend", "דחוף", "", "a  b"]) == ["Backend", "דחוף", "a b"]
    assert fold("שָׁלוֹם") == fold("שלום")


def test_tag_filter_and_or(repo, make_engine):
    write_task(repo, "A", tags=["ui", "דחוף"])
    write_task(repo, "B", tags=["ui"])
    write_task(repo, "C", tags=["api"])
    commit_all(repo)
    eng = make_engine()
    eng.refresh()
    ids = lambda ts: [t["id"] for t in ts]
    assert ids(eng.list_tasks(tags=["UI", "דחוף"])) == ["A"]
    assert ids(eng.list_tasks(tags=["ui", "api"], match="any")) == ["A", "B", "C"]
    assert {t["tag"]: t["count"] for t in eng.all_tags()} == {"ui": 2, "דחוף": 1, "api": 1}


def test_text_search_unicode(repo, make_engine):
    write_task(repo, "A", title="שָׁלוֹם עולם")
    write_task(repo, "B", title="Hello World")
    commit_all(repo)
    eng = make_engine()
    eng.refresh()
    assert [t["id"] for t in eng.list_tasks(q="שלום")] == ["A"]
    assert [t["id"] for t in eng.list_tasks(q="WORLD")] == ["B"]
    assert eng.list_tasks(q="%") == []


def test_cycle_detection():
    assert find_cycle({"a": ["b"], "b": ["c"], "c": ["a"]}) == ["a", "b", "c", "a"]
    # diamond is not a cycle
    assert find_cycle({"a": ["b", "c"], "b": ["d"], "c": ["d"]}) is None
    assert cycle_after_setting({"a": ["b"]}, "b", ["a"]) == ["a", "b", "a"]
    deep = {str(i): [str(i + 1)] for i in range(5000)}
    assert find_cycle(deep) is None


def test_legacy_flat_layout_still_read_and_written_in_place(repo, make_engine):
    legacy = repo / ".todo/tasks/OLD-1.json5"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"id": "OLD-1", "title": "flat"}))
    write_task(repo, "NEW-1", title="nested")
    commit_all(repo)
    eng = make_engine()
    eng.refresh()
    assert {t["id"]: t["title"] for t in eng.list_tasks()} == {"OLD-1": "flat", "NEW-1": "nested"}
    assert eng.task_path("OLD-1") == legacy
    assert eng.task_path("NEW-1") == repo / ".todo/tasks/NEW-1/main.json5"
    eng.save_task({"id": "OLD-1", "title": "flat edited"})  # stays in its own file
    assert json.loads(legacy.read_text())["title"] == "flat edited"
    assert not (repo / ".todo/tasks/OLD-1/main.json5").exists()
    eng.add_comment("OLD-1", "works beside the flat file")
    assert len(eng.get_comments("OLD-1")) == 1
    legacy.write_text(json.dumps({"id": "OLD-1", "title": "by hand"}))  # edit detected
    eng.refresh()
    assert eng.get_task("OLD-1")["title"] == "by hand"


def test_nested_file_wins_over_legacy(repo, make_engine):
    write_task(repo, "T-1", title="nested")
    (repo / ".todo/tasks/T-1.json5").write_text(json.dumps({"id": "T-1", "title": "flat"}))
    eng = make_engine()
    eng.refresh()
    assert eng.get_task("T-1")["title"] == "nested"
    eng.delete_task("T-1")
    assert (repo / ".todo/tasks/T-1.json5").exists()  # only the nested ticket and its dir go


def _flat(repo, tid, title="flat"):
    p = repo / f".todo/tasks/{tid}.json5"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"id": tid, "title": title}))
    return p


def test_migrate_layout(repo, make_engine):
    _flat(repo, "A-1")
    _flat(repo, "B-1")
    commit_all(repo)
    _flat(repo, "C-1")  # not tracked yet
    eng = make_engine()
    eng.add_comment("A-1", "keeps its comment")
    assert eng.legacy_tasks() == ["A-1", "B-1", "C-1"]
    res = eng.migrate_layout()
    assert res == {"moved": ["A-1", "B-1", "C-1"], "duplicates": [], "conflicts": []}
    assert eng.legacy_tasks() == []
    for tid in ("A-1", "B-1", "C-1"):
        assert (repo / f".todo/tasks/{tid}/main.json5").exists()
        assert not (repo / f".todo/tasks/{tid}.json5").exists()
    assert {t["id"] for t in eng.list_tasks()} == {"A-1", "B-1", "C-1"}
    assert len(eng.get_comments("A-1")) == 1
    git(repo, "add", "-A")  # tracked files show up as renames, history follows
    assert "R  .todo/tasks/A-1.json5 -> .todo/tasks/A-1/main.json5" in git(repo, "status", "--short")
    assert eng.migrate_layout() == {"moved": [], "duplicates": [], "conflicts": []}  # idempotent


def test_migrate_duplicates_and_conflicts(repo, make_engine):
    write_task(repo, "D-1", title="same")
    (repo / ".todo/tasks/D-1.json5").write_text((repo / ".todo/tasks/D-1/main.json5").read_text())
    write_task(repo, "E-1", title="nested")
    _flat(repo, "E-1", "different")
    commit_all(repo)
    eng = make_engine()
    res = eng.migrate_layout()
    assert res == {"moved": [], "duplicates": ["D-1"], "conflicts": ["E-1"]}
    assert not (repo / ".todo/tasks/D-1.json5").exists()
    assert (repo / ".todo/tasks/E-1.json5").exists()  # left for a human
    assert eng.get_task("E-1")["title"] == "nested"


# ---- a multi-line description typed straight into the file ---------------------------------
def test_raw_newlines_in_strings_are_tolerated(repo, make_engine):
    p = repo / ".todo/tasks/ML-1/main.json5"
    p.parent.mkdir(parents=True)
    p.write_text('{\n  "id": "ML-1",\n  "title": "multi",\n  "description": "# Plan\n  line two\n\t| a | b |",\n}\n')
    eng = make_engine()
    eng.refresh()
    assert eng.get_task("ML-1")["description"] == "# Plan\n  line two\n\t| a | b |"
    assert not eng.warnings


def test_escape_raw_newlines():
    from engine import escape_raw_newlines as esc, loads_forgiving
    assert esc('{"a": "x\ny"}') == '{"a": "x\\ny"}'
    assert esc('{"a": "x\r\ny\tz"}') == '{"a": "x\\ny\\tz"}'              # CRLF -> one \n, tab escaped
    assert esc("{'a': 'x\ny'}") == "{'a': 'x\\ny'}"                         # single quotes too
    assert esc('{"a": "x\\ny"}') == '{"a": "x\\ny"}'                        # already escaped: untouched
    assert esc('{"a": "q\\"\nz"}') == '{"a": "q\\"\\nz"}'                   # an escaped quote does not end the string
    assert esc('{"a": 1}\n') == '{"a": 1}\n'                               # newlines outside strings stay
    assert esc('{ // it\'s a comment\n"a": "x\ny" }') == '{ // it\'s a comment\n"a": "x\\ny" }'  # an apostrophe in a comment
    assert esc('{ /* "unclosed\n */ "a": "b\nc" }') == '{ /* "unclosed\n */ "a": "b\\nc" }'
    assert esc('{"a": "one \\\ntwo"}') == '{"a": "one \\\ntwo"}'          # backslash-newline continuation is valid
    assert loads_forgiving('{"a": "x\ny"}') == {"a": "x\ny"}
    assert loads_forgiving('{"a": "one \\\ntwo"}') == {"a": "one two"}
    import pytest
    with pytest.raises(ValueError):
        loads_forgiving("{ nope")                                           # real errors are still errors



# ---- a file that cannot be read is looked at again, never marked as done --------------------
def test_unreadable_files_are_retried_and_their_warning_clears(repo, make_engine, monkeypatch):
    import engine as E
    import json5
    write_task(repo, "OK-1", title="fine")
    bad = repo / ".todo/tasks/ML-1/main.json5"
    bad.parent.mkdir(parents=True)
    bad.write_text('{"id": "ML-1", "title": "multi", "description": "a\nb"}')
    commit_all(repo)
    good_loader = E.loads_forgiving
    monkeypatch.setattr(E, "loads_forgiving", json5.loads)  # a loader that cannot read it
    eng = make_engine()
    eng.refresh()
    assert [t["id"] for t in eng.list_tasks()] == ["OK-1"] and len(eng.warnings) == 1
    eng.refresh()
    eng.refresh()
    assert len(eng.warnings) == 1                       # looked at again, but not warned about again
    monkeypatch.setattr(E, "loads_forgiving", good_loader)  # the file is now readable, unchanged
    eng.refresh()
    assert {t["id"] for t in eng.list_tasks()} == {"OK-1", "ML-1"} and eng.warnings == []
    eng.refresh()  # and the state is clean again: a plain cache hit
    assert make_engine().db.execute("SELECT dirty_key FROM projects").fetchone()[0] == ""


def test_retry_state_survives_a_new_process(repo, make_engine, monkeypatch, db_path):
    import engine as E
    import json5
    write_task(repo, "T-1", title="v1")
    commit_all(repo)
    make_engine().refresh()
    p = repo / ".todo/tasks/T-1/main.json5"
    p.write_text('{"id": "T-1", "title": "v2", "description": "x\ny"}')  # now unreadable for a strict loader
    commit_all(repo)
    good = E.loads_forgiving
    monkeypatch.setattr(E, "loads_forgiving", json5.loads)
    first = make_engine()
    first.refresh()
    assert first.get_task("T-1")["title"] == "v1" and first.warnings        # the old row stays, with a warning
    monkeypatch.setattr(E, "loads_forgiving", good)
    second = make_engine()  # a fresh Engine (the next CLI command) on the same cache
    second.refresh()
    assert second.get_task("T-1")["title"] == "v2" and second.warnings == []


def test_a_bad_file_that_is_then_fixed_clears_its_warning(repo, make_engine):
    write_task(repo, "T-2", title="ok")
    commit_all(repo)
    eng = make_engine()
    eng.refresh()
    p = repo / ".todo/tasks/T-2/main.json5"
    p.write_text("{ nope")
    eng.refresh()
    assert len(eng.warnings) == 1 and eng.get_task("T-2")["title"] == "ok"
    p.write_text('{"id": "T-2", "title": "fixed"}')
    eng.refresh()
    assert eng.warnings == [] and eng.get_task("T-2")["title"] == "fixed"
