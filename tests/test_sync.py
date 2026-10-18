import pytest

from conftest import commit_all, git, init_repo, write_task
from engine import Engine, SyncError


@pytest.fixture
def world(tmp_path):
    """A bare remote and two clones (alice, bob) with one shared task."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    alice = init_repo(tmp_path / "alice")
    write_task(alice, "TKT-1", title="shared")
    commit_all(alice)
    git(alice, "remote", "add", "origin", str(remote))
    git(alice, "push", "-q", "-u", "origin", "main")
    git(tmp_path, "clone", "-q", str(remote), str(tmp_path / "bob"))
    bob = tmp_path / "bob"
    git(bob, "config", "user.name", "Bob")
    git(bob, "config", "user.email", "bob@example.com")
    mk = lambda repo, name: Engine(repo, db_path=tmp_path / f"{name}.sqlite3")
    a, b = mk(alice, "a"), mk(bob, "b")
    a.refresh(); b.refresh()
    return a, b, tmp_path


def test_save_then_update(world):
    a, b, _ = world
    a.save_task({"id": "TKT-2", "title": "from alice"}, create=True)
    a.add_comment("TKT-1", "hello")
    assert a.sync_status()["changes"] >= 2
    res = a.save_work()
    assert res["committed"] and res["pushed"]
    assert res["message"] == "tasks: update 2 task(s)"
    assert a.sync_status()["ahead"] == 0

    s = b.pull()
    assert s["tasks_added"] == 1 and s["comments_changed"] == 1 and not s["full"]
    assert {t["id"] for t in b.list_tasks()} == {"TKT-1", "TKT-2"}
    assert len(b.get_comments("TKT-1")) == 1


def test_save_only_stages_todo(world):
    a, _, _ = world
    (a.root / "src.py").write_text("x")
    a.save_task({"id": "TKT-1", "status": "done"})
    a.save_work("my message")
    assert git(a.root, "log", "-1", "--format=%s") == "my message"
    assert "?? src.py" in git(a.root, "status", "--porcelain")


def test_rejected_push_then_diverged_then_rebase(world):
    a, b, _ = world
    a.save_task({"id": "A", "title": "a"}, create=True); a.save_work()
    b.save_task({"id": "B", "title": "b"}, create=True)
    with pytest.raises(SyncError) as e:
        b.save_work()
    assert e.value.code == "rejected"
    with pytest.raises(SyncError) as e:
        b.pull()
    assert e.value.code == "diverged"
    b.pull(rebase=True)
    assert {t["id"] for t in b.list_tasks()} == {"TKT-1", "A", "B"}
    assert b.save_work()["pushed"]


def test_rebase_conflict_is_aborted(world):
    a, b, _ = world
    a.save_task({"id": "TKT-1", "title": "alice title"}); a.save_work()
    b.save_task({"id": "TKT-1", "title": "bob title"})
    git(b.root, "commit", "-qam", "bob")
    with pytest.raises(SyncError) as e:
        b.pull(rebase=True)
    assert e.value.code == "conflict"
    assert e.value.files == [".todo/tasks/TKT-1.json5"]
    assert git(b.root, "status", "--porcelain") == ""
    assert not (b.root / ".git/rebase-merge").exists()
    assert b.get_task("TKT-1")["title"] == "bob title"


def test_update_with_uncommitted_local_edit(world):
    a, b, _ = world
    a.save_task({"id": "X", "title": "x"}, create=True); a.save_work()
    b.save_task({"id": "TKT-1", "title": "local edit"})
    b.pull()
    assert b.get_task("X")["title"] == "x"
    assert b.get_task("TKT-1")["title"] == "local edit"


def test_no_upstream(repo, db_path):
    write_task(repo, "T")
    commit_all(repo)
    e = Engine(repo, db_path=db_path)
    e.refresh()
    with pytest.raises(SyncError) as err:
        e.pull()
    assert err.value.code == "no_upstream"
    with pytest.raises(SyncError) as err:
        e.save_work()
    assert err.value.code == "no_remote"


def test_set_upstream(world, tmp_path):
    a, _, _ = world
    git(a.root, "checkout", "-q", "-b", "feature")
    a.save_task({"id": "F", "title": "f"}, create=True)
    with pytest.raises(SyncError) as e:
        a.save_work()
    assert e.value.code == "no_upstream"
    assert a.save_work(set_upstream=True)["pushed"]
    assert a.sync_status()["upstream"] == "origin/feature"
