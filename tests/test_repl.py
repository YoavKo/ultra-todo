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
    assert comp(eng, "h") == ["h", "help"] and comp(eng, "ex") == ["exit"]


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


def prompts_session(repo, db_path, lines, capsys):
    """Like run_session, but also returns the prompts that were shown."""
    it, shown = iter(lines), []

    def fake_input(prompt):
        shown.append(prompt)
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    repl.run_repl(str(repo), "utd", db_path=db_path, registry=repo.parent / "repos.json",
                  input_fn=fake_input, interactive=False)
    return shown, capsys.readouterr()


@pytest.fixture
def psession(repo, db_path, capsys, monkeypatch):
    write_task(repo, "TKT-101", title="Login bug", priority="high")
    write_task(repo, "TKT-102", title="Settings")
    commit_all(repo)
    monkeypatch.setenv("COLUMNS", "100")
    return lambda *lines: prompts_session(repo, db_path, list(lines), capsys)


def test_prompt_and_current_ticket(psession):
    shown, r = psession("cd tkt-101", "show", "start", "priority low", "title Renamed here", "cd ..", "TKT-101",
                        "cd 102", "done", "cd", "ls --ids")
    assert shown[:2] == ["proj> ", "proj/TKT-101> "]            # the prompt names the ticket
    assert "proj/TKT-102> " in shown and shown[-2:] == ["proj> ", "proj> "]
    assert "TKT-101  Login bug" in r.out                        # show without an id: the current ticket
    assert "TKT-101: to do → in progress" in r.out
    assert "priority high → low" in r.out and "title → Renamed here" in r.out  # bare actions hit the current ticket
    assert "TKT-102: to do → done" in r.out                     # `cd 102` takes a bare number
    assert r.out.split()[-2:] == ["TKT-102", "TKT-101"]         # back at the repo: ls works again (101 is now low)


def test_current_ticket_errors_and_other_tickets(psession):
    shown, r = psession("show", "cd nope", "cd 101", "TKT-102 start", "show TKT-102", "cd a b", "info", "show --json")
    assert "no ticket selected" in r.err and "no ticket 'nope'" in r.err and "usage: cd" in r.err
    assert "TKT-102: to do → in progress" in r.out              # another id still works while one is current
    assert "Settings" in r.out and '"id": "TKT-101"' in r.out  # `show --json` is about the current ticket
    assert shown[-1] == "proj/TKT-101> "                        # errors don't change the current ticket


def test_rm_leaves_the_current_ticket(psession):
    shown, r = psession("cd TKT-102", "rm -y", "ls --ids")
    assert "TKT-102: deleted" in r.out and "TKT-102 no longer exists" in r.err
    assert shown[-1] == "proj> "                                # back to the repo prompt
    assert r.out.strip().endswith("TKT-101")


def test_use_resets_the_current_ticket(psession, tmp_path):
    from conftest import init_repo
    other = init_repo(tmp_path / "other")
    write_task(other, "OTH-1", title="x")
    commit_all(other)
    shown, r = psession("cd TKT-101", f"use {other}", "ls --ids")
    assert shown[-1] == "other> " and "OTH-1" in r.out


@pytest.mark.parametrize("word", ["exit", "quit", "q"])
def test_quit_words_leave(psession, word):
    shown, r = psession(word, "ls")  # nothing after it runs
    assert shown == ["proj> "] and r.out == ""


def test_q_still_leaves_inside_a_ticket(psession):
    shown, r = psession("cd TKT-101", "q", "ls")
    assert shown == ["proj> ", "proj/TKT-101> "]


def test_completion_for_the_current_ticket(eng):
    cur = "TKT-101"
    def c(line):
        words = line.split(" ")
        return repl.complete(eng, words[:-1], words[-1], current=cur)
    assert "done" in c("") and "TKT-102" in c("") and "cd" in c("") and "ls" in c("")  # actions + commands + ids
    assert c("-") == sorted(repl.TICKET_FLAGS)
    assert c("sta") == ["start", "status"]
    assert c("priority ") == ["high", "low", "medium"]
    assert c("blocked ") == ["ABC-7", "TKT-102"]                # never the current ticket itself
    assert c("open ") == ["1", "2", "3", "log.txt", "note.md", "shot.png"]
    assert c("cd ") == ["..", "ABC-7", "TKT-101", "TKT-102"]
    assert c("cd T") == ["TKT-101", "TKT-102"] and c("show ") == ["ABC-7", "TKT-101", "TKT-102"]
    assert c("ls --sort ") == sorted(cli.SORT_FIELDS) and "3d" in c("ls --since ")
    assert "--blocked" in c("ls --b") and "--csv" in c("ls --c") and "--md" in c("board --")
    assert repl.complete(eng, ["q"], "") == []                  # nothing after a quit word


def test_completion_for_tag_assign_title_rm(eng):
    def c(line, current=None):
        words = line.split(" ")
        return repl.complete(eng, words[:-1], words[-1], current=current)
    assert c("TKT-101 tag +") == ["+auth", "+backend", "+ui"]
    assert c("TKT-101 tag -") == ["-auth", "-backend"]          # only this ticket's tags can be removed
    assert c("TKT-101 tag +ui -a") == ["-auth"]
    assert "me" in c("TKT-101 assign ") and "alice" in c("TKT-101 assign ")
    assert c("TKT-101 assign alice ") == [] and c("TKT-101 title ") == []
    assert c("TKT-101 rm ") == ["--yes", "-y"] and c("TKT-101 rm -") == ["--yes", "-y"]


def test_help_mentions_the_new_things(psession):
    shown, r = psession("help")
    for word in ("cd ID", "show [ID]", "exit | quit | q", "--blocked", "--since", "deps", "rm [-y]"):
        assert word in r.out


def test_utd_alone_opens_the_repl_inside_a_repo(repo, monkeypatch, capsys, tmp_path):
    import app
    write_task(repo, "TKT-101", title="x")
    commit_all(repo)
    called = []
    monkeypatch.setenv("ULTRA_TODO_PROG", "utd")
    monkeypatch.setattr("repl.run_repl", lambda path, p, **kw: called.append(path))
    monkeypatch.chdir(repo)
    app.main([])
    assert [str(__import__("pathlib").Path(c).resolve()) for c in called] == [str(repo)]
    monkeypatch.chdir(repo / ".todo")  # a subdirectory of the repo counts too
    called.clear()
    app.main([])
    assert len(called) == 1
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)  # not a repo: help, not the repl
    called.clear()
    with pytest.raises(SystemExit) as e:
        app.main([])
    assert e.value.code == 0 and not called and "commands:" in capsys.readouterr().out
    from conftest import init_repo
    empty = init_repo(tmp_path / "notodo")  # a repo without .todo/tasks: help too
    monkeypatch.chdir(empty)
    with pytest.raises(SystemExit):
        app.main([])
    assert not called


def test_help_aliases_h_and_question_mark(psession):
    shown, r = psession("h", "help", "?")
    assert r.out.count("commands (type them without the repo name") == 3
    assert "short forms" in r.out and "l list=ls" in r.out and "d=done" in r.out


def test_short_forms_in_the_repl(psession):
    shown, r = psession("l --ids", "n --ids", "i --json", "a 'Added short' -p h", "b --md", "i",
                        "cd 101", "d", "st doing", "pr low", "t New name", "tg +x", "dp", "..", "q")
    out = r.out
    assert out.startswith("TKT-101\nTKT-102\n")                   # l = ls
    assert "created TKT-103  Added short" in out                  # a = new
    assert "## To do" in out and "## In progress" in out         # b = board (--md)
    assert "TKT-101: to do → done" in out                         # d = done on the current ticket
    assert "TKT-101: done → in progress" in out                   # st doing
    assert "priority high → low" in out and "title → New name" in out and "tags → #x" in out
    assert "not blocked by anything" in out                       # dp = deps
    assert shown[-2:] == ["proj/TKT-101> ", "proj> "]             # .. = cd ..


def test_a_ticket_id_beats_a_short_form(repo, db_path, capsys, monkeypatch):
    write_task(repo, "d", title="A ticket named d")
    write_task(repo, "TKT-1", title="other")
    commit_all(repo)
    monkeypatch.setenv("COLUMNS", "100")
    shown, r = prompts_session(repo, db_path, ["d", "cd d", "d", "done", "TKT-1 d", "q"], capsys)
    assert r.out.count("A ticket named d") == 2                   # `d` shows the ticket, even inside it
    assert "d: to do → done" in r.out                             # `done` is the unambiguous action
    assert "TKT-1: to do → done" in r.out                         # on another ticket `d` is the action


def test_completion_understands_short_forms(eng):
    def c(line, current=None):
        words = line.split(" ")
        return repl.complete(eng, words[:-1], words[-1], current=current)
    assert c("l --st") == ["--status"] and "--csv" in c("l --c")
    assert c("TKT-101 st ") == ["blocked", "doing", "done", "todo"]
    assert c("TKT-101 pr ") == ["high", "low", "medium"]
    assert c("TKT-102 bb ") == ["ABC-7", "TKT-101"]
    assert c("st ", current="TKT-101") == ["blocked", "doing", "done", "todo"]
    assert c("tg +", current="TKT-101") == ["+auth", "+backend", "+ui"]


def test_serve_and_open_are_refused_in_the_repl_but_open_works_on_a_ticket(psession, tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(cli, "launch", lambda path: opened.append(path.name))
    f = tmp_path / "a.txt"
    f.write_text("x")
    shown, r = psession("serve", "open", f"TKT-101 attach {f}", "cd TKT-101", "open", "q")
    assert r.err.count("is not available here") == 2 and "run it from the shell: utd open [REPO]" in r.err
    assert "in a ticket, open opens its files" in r.err
    assert opened == ["a.txt"]  # inside a ticket, `open` is the attachment action
