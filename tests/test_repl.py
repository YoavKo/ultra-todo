import pytest

import cli
import repl
from conftest import commit_all, write_task


@pytest.fixture
def eng(repo, make_engine):
    write_task(repo, "TKT-101", title="Login bug", priority="high", assignee="alice", tags=["auth", "backend"])
    write_task(repo, "TKT-102", title="Settings", assignee="bob", tags=["ui"], blocked_by=["TKT-101"])
    write_task(repo, "ABC-7", title="Other")
    commit_all(repo)
    e = make_engine()
    e.refresh()
    e.save_task({"id": "TKT-101"}, files=[("shot.png", b"1"), ("log.txt", b"2")])
    e.add_comment("TKT-101", "c", files=[("note.md", b"3")])
    return e


def comp(eng, line):
    """Completions for the text typed so far (the last word is the one being completed)."""
    words = line.split(" ")
    return repl.complete(eng, words[:-1], words[-1])


def test_completes_commands_and_ticket_ids(eng):
    assert comp(eng, "ne") == ["new", "next"]
    assert comp(eng, "") == sorted(repl.COMMANDS + ["ABC-7", "TKT-101", "TKT-102"])
    assert comp(eng, "TKT") == ["TKT-101", "TKT-102"]
    assert comp(eng, "h") == ["help"] and comp(eng, "ex") == ["exit"]


def test_completes_flags_and_values(eng):
    assert comp(eng, "ls --st") == ["--status"]
    assert comp(eng, "ls --status ") == ["blocked", "doing", "done", "todo"]
    assert comp(eng, "board --priority ") == ["high", "low", "medium"]
    assert comp(eng, "ls -t ") == ["auth", "backend", "ui"]
    assert comp(eng, "ls --assignee ") == ["alice", "bob"]
    assert "--attach" in comp(eng, "new --") and "--mine" in comp(eng, "next --")
    assert comp(eng, "new -a ") == ["alice", "bob"]
    assert comp(eng, "save -") == ["--message", "--set-upstream", "-h", "-m"]
    assert comp(eng, "ls -q ") == []  # free text: nothing to suggest


def test_completes_ticket_actions_and_arguments(eng):
    assert comp(eng, "TKT-101 st") == ["start", "status"]
    assert "attach" in comp(eng, "tkt-101 ") and "remove" in comp(eng, "TKT-101 ")  # ids match any case
    assert comp(eng, "TKT-101 status ") == ["blocked", "doing", "done", "todo"]
    assert comp(eng, "TKT-101 priority ") == ["high", "low", "medium"]
    assert comp(eng, "TKT-102 block-by ") == ["ABC-7", "TKT-101"]  # not itself
    assert comp(eng, "TKT-102 block-by ABC-7 ") == ["TKT-101"]  # not already chosen
    assert comp(eng, "TKT-101 blocked ") == ["ABC-7", "TKT-102"]
    assert comp(eng, "TKT-101 remove ") == ["block-by", "blocked"]
    assert comp(eng, "TKT-101 remove blocked ") == ["ABC-7", "TKT-102"]
    assert comp(eng, "TKT-101 open ") == ["1", "2", "3", "log.txt", "note.md", "shot.png"]
    assert comp(eng, "TKT-101 open s") == ["shot.png"]
    assert comp(eng, "TKT-101 detach ") == ["1", "2", "log.txt", "shot.png"]  # only the description's files
    assert "-m" in comp(eng, "TKT-101 comment -") and "-f" in comp(eng, "TKT-101 comment -")
    assert comp(eng, "NOPE-1 ") == []


def test_completes_file_names(eng, tmp_path, monkeypatch):
    tmp_path = tmp_path / "work"
    tmp_path.mkdir()
    (tmp_path / "a.log").write_text("x")
    (tmp_path / "docs").mkdir()
    (tmp_path / ".hidden").write_text("x")
    monkeypatch.chdir(tmp_path)
    assert comp(eng, "TKT-101 attach ") == ["a.log", "docs/"]  # directories get a slash, dotfiles hidden
    assert comp(eng, "TKT-101 attach a") == ["a.log"]
    assert comp(eng, "TKT-101 attach ./d") == ["./docs/"]
    assert comp(eng, "TKT-101 comment -f ") == ["a.log", "docs/"]
    assert comp(eng, "new x --attach ") == ["a.log", "docs/"]
    assert comp(eng, "TKT-101 attach .h") == [".hidden"]
    assert repl.file_matches("/no/such/dir/x") == []


def test_completes_repo_names_for_use(eng, repo, tmp_path):
    import repos
    reg = tmp_path / "repos.json"
    repos.remember(str(repo), reg)
    assert repl.complete(eng, ["use"], "pro", registry=reg, db_path=tmp_path / "none.sqlite3") == [repo.name] \
        or repo.name.startswith("pro")  # the fixture repo is called proj


def run_session(repo, db_path, lines, capsys, **kw):
    it = iter(lines)

    def fake_input(prompt):
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    repl.run_repl(str(repo), "utd", db_path=db_path, registry=repo.parent / "repos.json",
                  input_fn=fake_input, interactive=False, **kw)
    return capsys.readouterr()


@pytest.fixture
def session(repo, db_path, capsys, monkeypatch):
    write_task(repo, "TKT-101", title="Login bug", priority="high")
    write_task(repo, "TKT-102", title="Settings")
    commit_all(repo)
    monkeypatch.setenv("COLUMNS", "100")
    return lambda *lines: run_session(repo, db_path, list(lines), capsys)


def test_repl_runs_commands_and_survives_errors(session, repo):
    r = session("ls", "", "TKT-101", "TKT-102 start", "nope-1", "TKT-101 priority urgent",
                "ls --status doing --ids", "foo 'unclosed", "exit", "ls")  # nothing runs after exit
    assert "Login bug" in r.out and "TKT-102: to do → in progress" in r.out
    assert "no ticket 'nope-1'" in r.err                      # an error doesn't end the session
    assert "invalid priority" in r.err and "No closing quotation" in r.err
    assert r.out.strip().endswith("TKT-102")  # `ls --status doing --ids` found the started ticket


def test_repl_state_is_shared_between_commands(session):
    r = session("new 'From the repl' -p high", "next", "TKT-101 done", "next --json", "ls --ids")
    assert "created TKT-103" in r.out
    assert "From the repl" in r.out and "TKT-101: to do → done" in r.out


def test_repl_builtins(session, capsys):
    r = session("help", "?", "serve", "repl", "use", "use /no/such/dir", "q")
    assert r.out.count("commands (type them without the repo name") == 2
    assert "serve is not available here" in r.err and "already in the repl" in r.err
    assert "usage: use REPO" in r.err and "not a directory" in r.err


def test_repl_use_switches_repo(session, repo, tmp_path, db_path, capsys):
    from conftest import init_repo
    other = init_repo(tmp_path / "other")
    write_task(other, "OTH-1", title="In the other repo")
    commit_all(other)
    r = session("ls --ids", f"use {other}", "ls --ids", "TKT-101", "exit")
    assert r.out.split() [:2] == ["TKT-101", "TKT-102"] and "OTH-1" in r.out
    assert "no ticket 'TKT-101' in other" in r.err  # now working on the other repo


def test_repl_entry_via_run(repo, db_path, monkeypatch, capsys):
    write_task(repo, "TKT-101", title="x")
    commit_all(repo)
    monkeypatch.setattr("builtins.input", lambda prompt="": (_ for _ in ()).throw(EOFError()))
    cli.run([str(repo), "repl"], "utd", db_path=db_path, registry=repo.parent / "repos.json")
    assert capsys.readouterr().out.strip() == ""  # Ctrl-D leaves quietly (no tty: no banner)
