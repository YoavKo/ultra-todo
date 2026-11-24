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
