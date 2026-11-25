"""Interactive mode: ``utd REPO repl``.

A prompt that takes the same commands as ``utd REPO …`` (without the repo), keeps the repo's
index open between commands, and completes commands, ticket ids, values, tags, attachments
and file names with Tab (needs the ``readline`` module; Linux and macOS have it).
"""
from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

import platformdirs

import cli
import repos
from engine import PRIORITIES, Engine, EngineError

COMMANDS = ["info", "ls", "board", "next", "new", "pull", "save", "migrate", "use", "help", "exit", "quit"]
TICKET_ACTIONS = ["edit", "comment", "status", "start", "done", "reopen", "priority", "attach",
                  "detach", "open", "blocked", "block-by", "remove"]
TICKET_FLAGS = ["-e", "-c", "-m", "-f", "-s", "-p", "-a", "-d", "-o", "--path", "-b", "-bb", "-rb",
                "-rbb", "--json", "-h"]
FILTER_FLAGS = ["--status", "--priority", "--assignee", "-t", "--tag", "--any", "-q", "--query",
                "--open", "--mine", "--json", "-h"]
FLAGS = {"ls": FILTER_FLAGS, "board": FILTER_FLAGS, "next": FILTER_FLAGS + ["-n"],
         "new": ["-p", "--priority", "-a", "--assignee", "-t", "--tag", "-d", "--description",
                 "--status", "--id", "--attach", "-h"],
         "pull": ["--rebase", "-h"], "save": ["-m", "--message", "--set-upstream", "-h"],
         "migrate": ["-h"], "info": ["--json", "-h"]}
STATUSES = ["todo", "doing", "done", "blocked"]
DEP_FLAGS = {"-b", "-bb", "-rb", "-rbb", "--blocked", "--block-by", "--remove-blocked",
             "--remove-block-by"}

HELP = """commands (type them without the repo name; Tab completes):
  info                       project info
  ls | board | next          tickets as a table / kanban board / what to work on (filters: --status
                             --priority --assignee -t TAG -q TEXT --open --mine)
  new [TITLE] [flags]        create a ticket (-p -a -t -d --status --attach)
  pull | save                Update / Save work (save -m MSG; --set-upstream)
  migrate                    move tickets to the <id>/main.json5 layout
  ID                         show a ticket; then: edit, comment (-m TEXT -f FILES), status VALUE,
                             start | done | reopen, priority VALUE, attach FILES, detach SPECS,
                             open [SPECS], blocked IDS, block-by IDS, remove blocked|block-by IDS
  use REPO                   switch to another repo (path, name or number)
  help                       this text
  exit | quit | Ctrl-D       leave
Any command accepts -h for its own help."""


def file_matches(cur: str) -> list[str]:
    head, _, prefix = cur.rpartition("/")
    head = head + "/" if "/" in cur else ""
    base = os.path.expanduser(head) or "."
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    out = []
    for n in names:
        if n.startswith(prefix) and (prefix or not n.startswith(".")):
            out.append(head + n + ("/" if os.path.isdir(os.path.join(base, n)) else ""))
    return out


def ticket_ids(engine: Engine) -> list[str]:
    return [r[0] for r in engine.db.execute("SELECT id FROM tasks WHERE project_path=? ORDER BY id",
                                            (engine.project,))]


def complete(engine: Engine, words: list[str], cur: str, registry=None, db_path=None) -> list[str]:
    """Completions for `cur` given the words already typed on the line."""
    def pick(options) -> list[str]:
        return sorted({o for o in options if o.startswith(cur)})

    ids = ticket_ids(engine)
    first = words[0] if words else None
    prev = words[-1] if words else None
    if first is None:
        return pick(COMMANDS + ids)
    if first == "use":
        return pick(r["name"] for r in repos.known(registry, db_path) if r["exists"])
    if first in FLAGS:  # ls, board, next, new, pull, save, ...
        if prev in ("--status",):
            return pick(STATUSES)
        if prev in ("--priority", "-p") :
            return pick(PRIORITIES)
        if prev in ("-t", "--tag"):
            return pick(t["tag"] for t in engine.all_tags())
        if prev == "--assignee" or (first == "new" and prev in ("-a", "--assignee")):
            return pick(engine.assignees())
        if prev == "--attach":
            return file_matches(cur)
        if prev in ("-m", "--message", "-q", "--query", "-d", "--description", "--id", "-n"):
            return []
        return pick(FLAGS[first]) if cur.startswith("-") or first in ("pull", "save", "migrate") else []
    # a ticket: ID [action ...]
    match = next((i for i in ids if i == first or i.lower() == first.lower()), None)
    if match is None:
        return []
    rest = words[1:]
    if prev in ("status", "-s", "--status"):
        return pick(STATUSES)
    if prev in ("priority", "-p", "--priority"):
        return pick(PRIORITIES)
    last = next((w for w in reversed(rest) if w in TICKET_ACTIONS or w.startswith("-")), None)
    if cur.startswith("-"):
        return pick(TICKET_FLAGS)
    if not rest:
        return pick(TICKET_ACTIONS)
    if prev == "remove":
        return pick(["blocked", "block-by"])
    if last in ("blocked", "block-by") or last in DEP_FLAGS or last == "remove":
        return pick(i for i in ids if i != match and i not in rest)
    if last in ("attach", "-a", "--attach", "-f", "--file"):
        return file_matches(cur)
    if last in ("detach", "-d", "--detach", "open", "-o", "--open"):
        have = cli.all_attachments(engine, match)
        if last in ("detach", "-d", "--detach"):
            have = [h for h in have if h[0] == cli.DESCRIPTION]
        names = [a["name"] for _, _, a in have]
        return pick(names + [str(n) for n in range(1, len(have) + 1)])
    return []


def setup_readline(get_completions):
    """Tab completion and history; returns a function to save history, or None."""
    try:
        import readline
    except ImportError:  # Windows
        return None
    hist = Path(platformdirs.user_data_dir("ultra-todo")) / "repl_history"
    try:
        readline.read_history_file(hist)
    except OSError:
        pass
    readline.set_completer_delims(" \t\n")
    state: dict = {"matches": []}

    def completer(text: str, n: int):
        if n == 0:
            before = readline.get_line_buffer()[:readline.get_begidx()]
            try:
                words = shlex.split(before)
            except ValueError:
                words = before.split()
            try:
                state["matches"] = get_completions(words, text)
            except Exception:  # never break the prompt over a completion problem
                state["matches"] = []
        return state["matches"][n] if n < len(state["matches"]) else None

    readline.set_completer(completer)
    if "libedit" in (readline.__doc__ or ""):  # macOS
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")
        readline.parse_and_bind("set show-all-if-ambiguous on")  # one Tab lists the options

    def save():
        try:
            hist.parent.mkdir(parents=True, exist_ok=True)
            readline.set_history_length(1000)
            readline.write_history_file(hist)
        except OSError:
            pass
    return save


def run_repl(path: str, p: str, db_path=None, registry=None, input_fn=None,
             interactive: bool | None = None) -> None:
    input_fn = input_fn or input
    engine = cli.open_engine(path, db_path)
    interactive = sys.stdin.isatty() if interactive is None else interactive
    save_history = setup_readline(
        lambda words, cur: complete(engine, words, cur, registry, db_path)) if interactive else None
    if interactive:
        print(f"{Path(engine.project).name}: type help for commands, Tab completes, "
              "exit or Ctrl-D to leave")
    try:
        while True:
            try:
                line = input_fn(f"{Path(engine.project).name}> ")
            except EOFError:
                print()
                break
            except KeyboardInterrupt:
                print()
                continue
            try:
                words = shlex.split(line)
            except ValueError as e:
                print(f"error: {e}", file=sys.stderr)
                continue
            if not words:
                continue
            cmd = words[0]
            if cmd in ("exit", "quit", "q"):
                break
            if cmd in ("help", "?"):
                print(HELP)
                continue
            if cmd == "serve":
                print("serve is not available here: run it from the shell: "
                      f"{p} {Path(engine.project).name} serve", file=sys.stderr)
                continue
            if cmd == "repl":
                print("already in the repl", file=sys.stderr)
                continue
            if cmd == "use":
                if len(words) != 2:
                    print("usage: use REPO", file=sys.stderr)
                    continue
                try:
                    engine = cli.open_engine(cli.resolve_repo(words[1], db_path, registry), db_path)
                except (EngineError, SystemExit) as e:
                    print(e.code if isinstance(e, SystemExit) else f"error: {e}", file=sys.stderr)
                continue
            try:
                engine.refresh()
                cli.dispatch(engine, words, p)
            except SystemExit as e:  # fail() and argparse exit; the prompt carries on
                if isinstance(e.code, str):
                    print(e.code, file=sys.stderr)
            except KeyboardInterrupt:
                print()
            except (EngineError, PermissionError, OSError) as e:
                print(f"error: {e}", file=sys.stderr)
    finally:
        if save_history:
            save_history()
