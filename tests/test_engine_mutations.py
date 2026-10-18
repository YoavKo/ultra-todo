import json

import pytest

from conftest import commit_all, git, write_task
from engine import CycleError, NotFound, ValidationError


@pytest.fixture
def eng(repo, make_engine):
    write_task(repo, "TKT-101", title="first")
    commit_all(repo)
    e = make_engine()
    e.refresh()
    return e


def test_create_writes_readable_file(eng, repo):
    t, err = eng.save_task({"title": "משימה חדשה", "tags": ["UI", "ui"]}, create=True)
    assert err is None and t["id"] == "TKT-102"
    text = (repo / ".todo/tasks/TKT-102.json5").read_text("utf-8")
    assert "משימה חדשה" in text and text.endswith("\n")
    assert json.loads(text)["tags"] == ["UI"]


def test_update_keeps_unknown_fields(eng, repo):
    p = repo / ".todo/tasks/TKT-101.json5"
    p.write_text(json.dumps({"id": "TKT-101", "title": "first", "estimate": 3}))
    eng.save_task({"id": "TKT-101", "status": "done"})
    data = json.loads(p.read_text())
    assert data["estimate"] == 3 and data["status"] == "done"


def test_validation(eng):
    with pytest.raises(ValidationError):
        eng.save_task({"id": "../evil", "title": "x"}, create=True)
    with pytest.raises(ValidationError):
        eng.save_task({"title": ""}, create=True)
    with pytest.raises(ValidationError):
        eng.save_task({"id": "TKT-101", "status": "nope"})
    with pytest.raises(ValidationError):
        eng.save_task({"id": "TKT-101", "blocked_by": ["TKT-101"]})
    with pytest.raises(ValidationError):
        eng.save_task({"id": "TKT-101", "blocked_by": ["MISSING"]})
    with pytest.raises(NotFound):
        eng.save_task({"id": "TKT-999", "title": "x"})


def test_cycle_rejected(eng):
    eng.save_task({"id": "B", "title": "b", "blocked_by": ["TKT-101"]}, create=True)
    with pytest.raises(CycleError) as e:
        eng.save_task({"id": "TKT-101", "blocked_by": ["B"]})
    assert e.value.cycle[0] == e.value.cycle[-1]


def test_delete_task_cleans_dependents(eng, repo):
    eng.save_task({"id": "B", "title": "b", "blocked_by": ["TKT-101"]}, create=True)
    eng.add_comment("TKT-101", "hi")
    eng.delete_task("TKT-101")
    assert not (repo / ".todo/tasks/TKT-101").exists()
    assert eng.get_task("B")["blocked_by"] == []
    assert json.loads((repo / ".todo/tasks/B.json5").read_text())["blocked_by"] == []


def test_auto_commit(repo, make_engine):
    e = make_engine(auto_commit=True)
    e.refresh()
    e.save_task({"id": "X", "title": "x"}, create=True)
    (repo / "unrelated.txt").write_text("u")
    e.save_task({"id": "X", "status": "done"})
    assert git(repo, "log", "-1", "--format=%s") == "task: update X"
    assert "unrelated.txt" in git(repo, "status", "--porcelain")
    assert not make_engine().refresh().changed  # state recorded after commit


def test_comments_order_edit_delete(eng, repo):
    c1, _ = eng.add_comment("TKT-101", "ראשון")
    c2, _ = eng.add_comment("TKT-101", "second")
    assert c1["author"] == "Tester <tester@example.com>"
    assert [c["id"] for c in eng.get_comments("TKT-101")] == [c2["id"], c1["id"]]
    assert eng.get_task("TKT-101")["comment_count"] == 2
    c, _ = eng.edit_comment("TKT-101", c1["id"], "edited")
    assert c["edited_at"] and c["created_at"] == c1["created_at"]
    eng.delete_comment("TKT-101", c2["id"])
    assert [c["body"] for c in eng.get_comments("TKT-101")] == ["edited"]
    with pytest.raises(ValidationError):
        eng.add_comment("TKT-101", "  ")
    assert eng.list_tasks(q="edited")[0]["id"] == "TKT-101"


def test_comment_other_author_forbidden(eng, repo, make_engine):
    c, _ = eng.add_comment("TKT-101", "mine")
    other = make_engine(author="Someone Else")
    with pytest.raises(PermissionError):
        other.edit_comment("TKT-101", c["id"], "hijack")


def test_attachments(eng, repo):
    c, _ = eng.add_comment("TKT-101", "", files=[("../../evil.png", b"\x89PNG"), ("evil.png", b"2")])
    names = [a["name"] for a in c["attachments"]]
    assert names == ["evil.png", "evil-2.png"]
    p = eng.attachment_path("TKT-101", c["id"], "evil.png")
    assert p.read_bytes() == b"\x89PNG"
    assert p.is_relative_to(repo / ".todo/tasks/TKT-101/attachments")
    with pytest.raises(NotFound):
        eng.attachment_path("TKT-101", c["id"], "../../../TKT-101.json5")
    eng.max_attachment_bytes = 1
    with pytest.raises(ValidationError):
        eng.add_comment("TKT-101", "x", files=[("big.bin", b"12")])
    c2, _ = eng.edit_comment("TKT-101", c["id"], "now text", remove=["evil.png"])
    assert [a["name"] for a in c2["attachments"]] == ["evil-2.png"]


def test_comment_deleted_via_git_removes_row(eng, repo):
    c, _ = eng.add_comment("TKT-101", "x")
    commit_all(repo)
    eng.refresh()
    (repo / f".todo/tasks/TKT-101/comments/{c['id']}.json5").unlink()
    commit_all(repo)
    eng.refresh()
    assert eng.get_comments("TKT-101") == []


def test_priority_default_validation_and_order(eng, repo):
    assert eng.get_task("TKT-101")["priority"] == "medium"  # file has no priority
    eng.save_task({"id": "L", "title": "l", "priority": "low"}, create=True)
    eng.save_task({"id": "H", "title": "h", "priority": "high"}, create=True)
    assert [t["id"] for t in eng.list_tasks()] == ["H", "TKT-101", "L"]
    assert [t["id"] for t in eng.list_tasks(priority="high")] == ["H"]
    assert json.loads((repo / ".todo/tasks/H.json5").read_text())["priority"] == "high"
    with pytest.raises(ValidationError):
        eng.save_task({"id": "H", "priority": "urgent"})
