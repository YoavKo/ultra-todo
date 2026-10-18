import pytest

import repos
from engine import EngineError


def test_registry_remember_list_resolve(tmp_path, repo, make_engine):
    reg, db = tmp_path / "repos.json", tmp_path / "none.sqlite3"
    other = tmp_path / "x" / repo.name
    other.mkdir(parents=True)
    repos.remember(str(repo), reg)
    assert [r["name"] for r in repos.known(reg, db)] == [repo.name]
    assert repos.resolve(repo.name.upper(), reg, db) == str(repo)
    with pytest.raises(EngineError, match="no known repo"):
        repos.resolve("nope", reg, db)
    repos.remember(str(other), reg)
    with pytest.raises(EngineError, match="several"):
        repos.resolve(repo.name, reg, db)
    other.rmdir()
    assert repos.resolve(repo.name, reg, db) == str(repo)  # missing dirs are skipped


def test_registry_imports_cached_projects(tmp_path, repo, make_engine):
    e = make_engine()
    e.refresh()
    reg = tmp_path / "repos.json"
    assert [r["path"] for r in repos.known(reg, e.db_path)] == [e.project]
    assert reg.exists()


def test_resolve_by_number(tmp_path, repo):
    reg, db = tmp_path / "repos.json", tmp_path / "none.sqlite3"
    other = tmp_path / "other"
    other.mkdir()
    repos.remember(str(repo), reg)
    repos.remember(str(other), reg)  # most recent: number 1
    assert repos.resolve("1", reg, db) == str(other)
    assert repos.resolve("2", reg, db) == str(repo)
    with pytest.raises(EngineError, match="no repo number 3"):
        repos.resolve("3", reg, db)
    other.rmdir()
    with pytest.raises(EngineError, match="missing"):
        repos.resolve("1", reg, db)


def test_forget_by_numbers_and_names(tmp_path, repo, make_engine):
    e = make_engine()
    e.refresh()
    reg = tmp_path / "repos.json"
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    for p in (repo, a, b):
        repos.remember(str(p), reg)
    # ls order: b(1), a(2), repo(3); numbers are resolved before anything is removed
    b.rmdir()  # missing repos can be forgotten too
    removed = repos.forget(["1", "3"], reg, e.db_path)
    assert {r["path"] for r in removed} == {str(b), str(repo)}
    assert [r["path"] for r in repos.known(reg, e.db_path)] == [str(a)]  # not re-imported
    assert e.db.execute("SELECT COUNT(*) FROM tasks WHERE project_path=?",
                        (e.project,)).fetchone()[0] == 0
    with pytest.raises(EngineError):
        repos.forget(["a", "9"], reg, e.db_path)
    assert [r["path"] for r in repos.known(reg, e.db_path)] == [str(a)]  # all or nothing
    assert repo.exists()
