import json

import pytest

import cli
import repos
from conftest import commit_all, write_task


@pytest.fixture
def proj(repo, db_path, monkeypatch):
    write_task(repo, "TKT-101", title="Login bug", status="in_progress", priority="high",
               assignee="alice", tags=["auth", "backend"], description="Bounced to /")
    write_task(repo, "TKT-102", title="Settings page", assignee="bob", tags=["ui"],
               blocked_by=["TKT-101"])
    write_task(repo, "TKT-103", title="Docs", priority="low", status="done")
    commit_all(repo)
    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)

    def go(*args, spec=None):
        return cli.run([spec or str(repo), *args], "utd", db_path=db_path,
                       registry=repo.parent / "repos.json")
    go.repo, go.db = repo, db_path
    return go


def out(capsys):
    return capsys.readouterr().out


def tasks(proj):
    from engine import Engine
    e = Engine(proj.repo, db_path=proj.db)
    e.refresh()
    return {t["id"]: t for t in e.list_tasks()}


def test_info(proj, capsys):
    proj()
    text = out(capsys)
    assert "tickets:    3" in text and "in progress 1" in text
    assert "alice 1" in text and "high 1" in text and "auth 1" in text
    proj("--json")
    info = json.loads(out(capsys))
    assert info["tickets"] == 3 and info["by_status"]["done"] == 1


def test_ls_and_filters(proj, capsys):
    proj("ls")
    text = out(capsys)
    assert text.index("TKT-101") < text.index("TKT-102") < text.index("TKT-103")  # priority order
    assert "3 tickets" in text and "▲ high" in text and "#auth #backend" in text
    proj("ls", "--status", "done")
    assert "TKT-103" in out(capsys)
    proj("ls", "-t", "auth", "-t", "ui")  # AND: nothing has both
    assert "no tickets" in out(capsys)
    proj("ls", "-t", "auth", "-t", "ui", "--any", "--ids")
    assert out(capsys).split() == ["TKT-101", "TKT-102"]
    proj("ls", "-q", "bounced", "--json")
    assert [t["id"] for t in json.loads(out(capsys))] == ["TKT-101"]


def test_board(proj, capsys, monkeypatch):
    proj("board")
    wide = out(capsys).splitlines()
    assert wide[0].split("  ")[0] == "To do (1)" and "In progress (1)" in wide[0]
    assert any("TKT-101 ▲ →1" in l for l in wide)
    assert any("TKT-102 ● ✖1" in l for l in wide)
    monkeypatch.setenv("COLUMNS", "50")
    proj("board")
    narrow = out(capsys)
    assert narrow.index("To do (1)") < narrow.index("In progress (1)") < narrow.index("Blocked (0)")
    assert max(len(l) for l in narrow.splitlines()) <= 50


def test_show_ticket_and_lookup(proj, capsys):
    proj("TKT-102")
    text = out(capsys)
    assert "Settings page" in text and "TKT-101  Login bug  [In progress]" in text
    for spec in ("tkt-102", "102"):
        proj(spec)
        assert "Settings page" in out(capsys)
    proj("TKT-101", "--json")
    assert json.loads(out(capsys))["blocks"] == ["TKT-102"]
    with pytest.raises(SystemExit, match="no ticket 'TKT-999'"):
        proj("TKT-999")


def test_repo_specs(proj, repo, db_path, capsys):
    proj("ls", spec=str(repo))
    assert "TKT-101" in out(capsys)
    repos.remember(str(repo), repo.parent / "repos.json")
    proj("ls", spec=repo.name.upper())
    assert "TKT-101" in out(capsys)
    with pytest.raises(SystemExit, match=r"no known repo.*\./nope"):
        proj(spec="nope")
    with pytest.raises(SystemExit, match="not a directory"):
        proj(spec="./nope")
    assert proj("serve", "--port", "9000") == ["--target", str(repo), "--port", "9000"]


def test_dependencies(proj, capsys):
    proj("TKT-103", "blocked", "101", "102")  # 103 blocks 101 and 102
    assert "TKT-101 is now blocked by TKT-103" in out(capsys)
    t = tasks(proj)
    assert t["TKT-101"]["blocked_by"] == ["TKT-103"]
    assert t["TKT-102"]["blocked_by"] == ["TKT-101", "TKT-103"]
    proj("TKT-103", "remove", "blocked", "101")
    proj("TKT-102", "-rbb", "TKT-103")
    t = tasks(proj)
    assert t["TKT-101"]["blocked_by"] == [] and t["TKT-102"]["blocked_by"] == ["TKT-101"]
    proj("TKT-103", "block-by", "TKT-102")
    assert tasks(proj)["TKT-103"]["blocked_by"] == ["TKT-102"]
    proj("TKT-103", "-rbb", "TKT-102")
    assert tasks(proj)["TKT-103"]["blocked_by"] == []
    out(capsys)
    with pytest.raises(SystemExit, match="cycle"):
        proj("TKT-102", "blocked", "TKT-101")  # 101 is already blocking 102
    with pytest.raises(SystemExit, match="block itself"):
        proj("TKT-101", "-bb", "101")
    with pytest.raises(SystemExit):  # needs an id
        proj("TKT-101", "remove", "blocked")


def test_remove_dangling_blocker(proj, repo):
    write_task(repo, "TKT-104", title="x", blocked_by=["GONE-1"])
    proj("TKT-104", "-rbb", "GONE-1")
    assert tasks(proj)["TKT-104"]["blocked_by"] == []


def test_priority(proj, capsys):
    proj("TKT-102", "priority", "high")
    assert "medium → high" in out(capsys)
    assert tasks(proj)["TKT-102"]["priority"] == "high"
    proj("TKT-102", "-p", "l")
    assert tasks(proj)["TKT-102"]["priority"] == "low"
    proj("TKT-102", "-p", "low")
    assert "already low" in out(capsys)
    with pytest.raises(SystemExit):
        proj("TKT-102", "priority", "urgent")


def test_comment(proj, capsys, monkeypatch):
    proj("TKT-101", "-c", "-m", "from the cli")
    assert "commented on TKT-101" in out(capsys)
    proj("TKT-101")
    assert "from the cli" in out(capsys)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("from stdin\n"))
    proj("TKT-101", "comment")
    out(capsys)
    proj("TKT-101", "--json")
    data = json.loads(out(capsys))
    assert sorted(c["body"] for c in data["comments"]) == ["from stdin", "from the cli"]
    assert "project_path" not in data
    with pytest.raises(SystemExit, match="empty"):
        proj("TKT-101", "comment", "-m", "   ")
    with pytest.raises(SystemExit):
        proj("TKT-101", "-m", "x")  # -m needs comment


def test_edit(proj, capsys, monkeypatch):
    path = proj.repo / ".todo" / "tasks" / "TKT-103" / "main.json5"
    monkeypatch.setenv("EDITOR", "sed -i s/Docs/Documentation/")
    proj("TKT-103", "edit")
    assert "updated" in out(capsys) and "Documentation" in path.read_text()
    assert tasks(proj)["TKT-103"]["title"] == "Documentation"
    monkeypatch.setenv("EDITOR", "true")
    proj("TKT-103", "-e")
    assert "no changes" in out(capsys)
    monkeypatch.setenv("EDITOR", "sed -i s/in_progress/bogus/;s/todo/bogus/;s/done/bogus/")
    with pytest.raises(SystemExit, match="not valid"):
        proj("TKT-103", "edit")
    monkeypatch.setenv("EDITOR", "sed -i s/TKT-103/TKT-999/")
    with pytest.raises(SystemExit, match="not valid"):
        proj("TKT-103", "edit")
    monkeypatch.setenv("EDITOR", "false")
    with pytest.raises(SystemExit, match="exited"):
        proj("TKT-103", "edit")


def test_new_ticket(proj, capsys, monkeypatch):
    proj("new", "Fix", "the", "thing", "-p", "h", "-a", "carol", "-t", "ui,api", "-t", "x",
         "-d", "details", "--status", "doing")
    assert "created TKT-104  Fix the thing" in out(capsys)
    t = tasks(proj)["TKT-104"]
    assert (t["priority"], t["assignee"], t["status"], t["description"]) == (
        "high", "carol", "in_progress", "details")
    assert sorted(t["tags"]) == ["api", "ui", "x"]
    proj("new", "Custom", "--id", "ABC-1")
    assert "created ABC-1" in out(capsys)
    with pytest.raises(SystemExit, match="already exists"):
        proj("new", "Dup", "--id", "ABC-1")
    monkeypatch.setenv("EDITOR", "sed -i '1s/^/From-editor\\n\\nbody-line/'")
    proj("new")
    assert "From-editor" in out(capsys)
    assert tasks(proj)["TKT-105"]["description"] == "body-line"
    monkeypatch.setenv("EDITOR", "true")
    with pytest.raises(SystemExit, match="empty title"):
        proj("new")
    with pytest.raises(SystemExit):
        proj("new", "x", "-p", "urgent")


def test_status(proj, capsys):
    proj("TKT-102", "start")
    assert "to do → in progress" in out(capsys)
    proj("TKT-102", "status", "done")
    proj("TKT-102", "-s", "doing")
    assert tasks(proj)["TKT-102"]["status"] == "in_progress"
    proj("TKT-102", "done")
    assert tasks(proj)["TKT-102"]["status"] == "done"
    proj("TKT-102", "reopen")
    assert tasks(proj)["TKT-102"]["status"] == "todo"
    proj("TKT-102", "-s", "blocked")
    assert tasks(proj)["TKT-102"]["status"] == "blocked"
    out(capsys)
    proj("TKT-102", "-s", "blocked")
    assert "already blocked" in out(capsys)
    for bad in (("status", "later"), ("status",), ("done", "now")):
        with pytest.raises(SystemExit):
            proj("TKT-102", *bad)


def test_ls_open_and_mine(proj, capsys):
    proj("ls", "--open", "--ids")
    assert out(capsys).split() == ["TKT-101", "TKT-102"]
    proj("ls", "--mine", "--ids")
    assert out(capsys).strip() == ""
    proj("TKT-103", "-s", "todo")
    out(capsys)
    from conftest import git
    git(proj.repo, "config", "user.name", "Bob")
    proj("ls", "--mine", "--ids")
    assert out(capsys).split() == ["TKT-102"]


def test_next(proj, capsys):
    proj("next")
    text = out(capsys)
    assert "TKT-101" in text and "TKT-102" not in text and "TKT-103" not in text  # 102 blocked, 103 done
    proj("TKT-101", "done")
    out(capsys)
    proj("next", "--json")
    assert [t["id"] for t in json.loads(out(capsys))] == ["TKT-102"]  # its blocker is done now
    proj("TKT-102", "done")
    out(capsys)
    proj("next")
    assert "nothing ready" in out(capsys)


@pytest.fixture
def remote_proj(tmp_path, monkeypatch):
    from conftest import git, init_repo
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    alice = init_repo(tmp_path / "alice")
    write_task(alice, "TKT-1", title="shared")
    commit_all(alice)
    monkeypatch.setenv("COLUMNS", "120")

    def go(repo, *args):
        return cli.run([str(repo), *args], "utd", db_path=tmp_path / "db.sqlite3",
                       registry=tmp_path / "repos.json")
    go.alice, go.remote, go.tmp = alice, remote, tmp_path
    return go


def test_save_and_pull(remote_proj, capsys):
    from conftest import git
    go, alice = remote_proj, remote_proj.alice
    with pytest.raises(SystemExit, match="no remote"):
        go(alice, "save")
    git(alice, "remote", "add", "origin", str(go.remote))
    go(alice, "new", "Second")
    out(capsys)
    with pytest.raises(SystemExit, match="no upstream.*--set-upstream"):
        go(alice, "save", "-m", "tasks: add second")  # commits locally, then cannot push
    go(alice, "save", "--set-upstream")
    assert out(capsys).splitlines() == ["nothing to commit", "pushed"]
    assert git(alice, "log", "-1", "--format=%s") == "tasks: add second"
    go(alice, "save")
    assert out(capsys).strip() == "nothing to commit"
    git(go.tmp, "clone", "-q", str(go.remote), str(go.tmp / "bob"))
    bob = go.tmp / "bob"
    git(bob, "config", "user.name", "Bob")
    go(bob, "TKT-1", "-c", "-m", "hi from bob")
    go(bob, "save", "-m", "bob comment")
    out(capsys)
    go(alice, "pull")
    assert "updated: 0 added, 0 changed, 0 removed, 1 comments" in out(capsys)
    go(alice, "pull")
    assert "already up to date" in out(capsys)
    go(alice, "new", "Third", "--id", "A-3")
    go(bob, "new", "Bobs", "--id", "B-1")
    go(bob, "save")
    out(capsys)
    with pytest.raises(SystemExit, match="newer changes.*pull first"):
        go(alice, "save")  # committed locally, but the push is rejected
    with pytest.raises(SystemExit, match="diverged.*pull --rebase"):
        go(alice, "pull")
    go(alice, "pull", "--rebase")
    go(alice, "save")
    assert "pushed" in out(capsys)


def test_attach_and_detach(proj, capsys, tmp_path):
    shot, note = tmp_path / "shot.png", tmp_path / "note.txt"
    shot.write_bytes(b"\x89PNG")
    note.write_text("hi")
    proj("TKT-101", "attach", str(shot), str(note))
    assert "TKT-101: attached shot.png, note.txt" in out(capsys)
    assert (proj.repo / ".todo/tasks/TKT-101/attachments/description/shot.png").exists()
    proj("TKT-101")
    text = out(capsys)
    assert "Attachments (2)" in text and "shot.png" in text
    assert ".todo/tasks/TKT-101/attachments/description/note.txt" in text
    proj("TKT-101", "attach", str(note))
    assert "attached note-2.txt" in out(capsys)  # no overwrite
    proj("TKT-101", "-d", "note.txt", "note-2.txt")
    assert "removed note.txt, note-2.txt" in out(capsys)
    proj("TKT-101", "--json")
    assert [a["name"] for a in json.loads(out(capsys))["attachments"]] == ["shot.png"]
    with pytest.raises(SystemExit, match=r"(?s)no attachment 'ghost.txt'.*shot.png"):
        proj("TKT-101", "detach", "ghost.txt")
    with pytest.raises(SystemExit, match="not a file"):
        proj("TKT-101", "attach", str(tmp_path / "nope"))
    with pytest.raises(SystemExit):
        proj("TKT-101", "attach")
    proj("new", "With file", "--attach", str(note))
    assert "created TKT-104" in out(capsys)
    assert [a["name"] for a in tasks(proj)["TKT-104"]["attachments"]] == ["note.txt"]


def test_migrate_command_and_info_hint(proj, capsys):
    flat = proj.repo / ".todo/tasks/OLD-1.json5"
    flat.write_text(json.dumps({"id": "OLD-1", "title": "flat one"}))
    proj()
    assert "1 ticket(s) use the old flat file layout" in out(capsys)
    proj("--json")
    assert json.loads(out(capsys))["legacy_layout_tickets"] == 1
    proj("migrate")
    text = out(capsys)
    assert "moved 1 ticket(s)" in text and "not committed yet" in text
    assert (proj.repo / ".todo/tasks/OLD-1/main.json5").exists() and not flat.exists()
    proj()
    assert "old flat file layout" not in out(capsys)
    proj("migrate")
    assert "nothing to migrate" in out(capsys)
    flat.write_text(json.dumps({"id": "OLD-1", "title": "other"}))
    with pytest.raises(SystemExit, match="different contents.*OLD-1"):
        proj("migrate")


def test_open_attachments(proj, capsys, tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(cli, "launch", lambda path: opened.append(path))
    shot, note = tmp_path / "shot.png", tmp_path / "note.txt"
    shot.write_bytes(b"\x89PNG")
    note.write_text("hi")
    with pytest.raises(SystemExit, match="no attachments"):
        proj("TKT-101", "open")
    proj("TKT-101", "attach", str(shot))
    c, _ = __import__("engine").Engine(proj.repo, db_path=proj.db).add_comment(
        "TKT-101", "see file", files=[("note.txt", b"in a comment"), ("shot.png", b"other")])
    out(capsys)
    proj("TKT-101", "open", "note.txt")  # found in a comment
    assert opened == [proj.repo / f".todo/tasks/TKT-101/attachments/{c['id']}/note.txt"]
    assert "opening .todo/tasks/TKT-101/attachments/" in out(capsys)
    with pytest.raises(SystemExit, match=r"(?s)several attachments.*1  shot.png.*3  shot.png"):
        proj("TKT-101", "open", "shot.png")  # in the description and in the comment
    opened.clear()
    proj("TKT-101", "-o", "description/shot.png", f"{c['id']}/shot.png")
    assert [p.name for p in opened] == ["shot.png", "shot.png"] and opened[0] != opened[1]
    out(capsys)
    proj("TKT-101", "open", "note.txt", "--path")
    assert out(capsys).strip().endswith(f"{c['id']}/note.txt") and len(opened) == 2  # printed, not launched
    with pytest.raises(SystemExit, match=r"(?s)no attachment 'ghost'.*note.txt"):
        proj("TKT-101", "open", "ghost")
    with pytest.raises(SystemExit, match=r"(?s)has 3 attachments.*2  note.txt"):
        proj("TKT-101", "open")  # several, not a terminal: lists them
    proj("TKT-102", "attach", str(note))
    out(capsys)
    opened.clear()
    proj("TKT-102", "open")  # a single attachment needs no name
    assert [p.name for p in opened] == ["note.txt"]


def test_launch_without_opener(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit, match="xdg-open not found.*--path"):
        cli.launch(tmp_path / "x")
    calls = []
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda cmd, **kw: calls.append(cmd))
    cli.launch(tmp_path / "x")
    assert calls == [["xdg-open", str(tmp_path / "x")]]
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    cli.launch(tmp_path / "y")
    assert calls[-1][0] == "open"


def test_attachment_numbers(proj, capsys, tmp_path, monkeypatch):
    from engine import Engine
    opened = []
    monkeypatch.setattr(cli, "launch", lambda path: opened.append(path.name))
    a, b = tmp_path / "a.png", tmp_path / "b.txt"
    a.write_bytes(b"1")
    b.write_text("2")
    proj("TKT-101", "attach", str(a))
    eng = Engine(proj.repo, db_path=proj.db)
    eng.add_comment("TKT-101", "first", files=[("c.log", b"3")])
    eng.add_comment("TKT-101", "second", files=[("d.log", b"4"), ("e.log", b"5")])
    out(capsys)
    proj("TKT-101")
    text = out(capsys)  # numbered: description first, then comments oldest first
    assert "1  a.png" in text and "attachment 2: c.log" in text
    assert "attachment 3: d.log" in text and "attachment 4: e.log" in text
    proj("TKT-101", "open", "2", "4")
    assert opened == ["c.log", "e.log"]
    proj("TKT-101", "open", "1", "--path")
    assert out(capsys).strip().endswith("attachments/description/a.png")
    # a new comment never renumbers the existing ones
    eng.add_comment("TKT-101", "third", files=[("f.log", b"6")])
    proj("TKT-101", "open", "2")
    assert opened[-1] == "c.log"
    with pytest.raises(SystemExit, match=r"no attachment number 9 \(it has 1-5\)"):
        proj("TKT-101", "open", "9")
    with pytest.raises(SystemExit, match="belongs to a comment"):
        proj("TKT-101", "detach", "2")
    proj("TKT-101", "detach", "1")  # description attachment by number
    assert "removed a.png" in out(capsys)
    out(capsys)
    # a file literally named like a number wins over the index
    proj("TKT-101", "attach", str(b))
    eng.save_task({"id": "TKT-101"}, files=[("2", b"named two")])
    opened.clear()
    proj("TKT-101", "open", "2")
    assert opened == ["2"]
    # no name, several files: a terminal gets a prompt
    monkeypatch.setattr("sys.stdin", type("TTY", (), {"isatty": lambda self: True})())
    monkeypatch.setattr(cli, "ask", lambda prompt: "b.txt")
    opened.clear()
    proj("TKT-101", "open")
    shown = out(capsys)
    assert opened == ["b.txt"] and "1  b.txt" in shown and "3  c.log" in shown  # the list was shown first
    monkeypatch.setattr(cli, "ask", lambda prompt: "")
    with pytest.raises(SystemExit, match="nothing chosen"):
        proj("TKT-101", "open")


def test_comment_with_files(proj, capsys, tmp_path, monkeypatch):
    f1, f2 = tmp_path / "trace.txt", tmp_path / "shot.png"
    f1.write_text("boom")
    f2.write_bytes(b"\x89PNG")
    monkeypatch.setattr("sys.stdin", type("TTY", (), {"isatty": lambda self: True})())
    proj("TKT-101", "comment", "-m", "see logs", "-f", str(f1), str(f2))
    assert "2 file(s): trace.txt, shot.png" in out(capsys)
    c = __import__("engine").Engine(proj.repo, db_path=proj.db).get_comments("TKT-101")[0]
    assert c["body"] == "see logs" and [a["name"] for a in c["attachments"]] == ["trace.txt", "shot.png"]
    assert (proj.repo / f".todo/tasks/TKT-101/attachments/{c['id']}/trace.txt").read_text() == "boom"
    proj("TKT-101", "-c", "-f", str(f1))  # files only: no editor, no text
    assert "1 file(s): trace.txt" in out(capsys)
    with pytest.raises(SystemExit, match="not a file"):
        proj("TKT-101", "-c", "-m", "x", "-f", str(tmp_path / "nope"))
    with pytest.raises(SystemExit):
        proj("TKT-101", "-f", str(f1))  # -f needs comment
    out(capsys)
    proj("TKT-101")
    view = out(capsys)  # numbered oldest comment first: 1-2 in "see logs", 3 in the files-only comment
    assert "attachment 1: trace.txt" in view and "attachment 2: shot.png" in view
    assert "attachment 3: trace.txt" in view
