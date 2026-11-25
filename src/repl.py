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

COMMANDS = ["info", "ls", "board", "next", "new", "pull", "save", "migrate", "use", "cd", "show",
            "help", "h", "exit", "quit", "q"]
TICKET_ACTIONS = ["edit", "comment", "title", "assign", "tag", "rm", "delete", "deps", "status", "start",
                  "done", "reopen", "priority", "attach", "detach", "open", "blocked", "block-by", "remove"]
RAW_ACTIONS = ("title", "assign", "tag", "rm", "delete")
SINCE_HINTS = ["1h", "6h", "1d", "3d", "1w", "2w"]
TICKET_FLAGS = ["-e", "-c", "-m", "-f", "-s", "-p", "-a", "-d", "-o", "--path", "-b", "-bb", "-rb",
                "-rbb", "--json", "-h"]
FILTER_FLAGS = ["--status", "--priority", "--assignee", "-t", "--tag", "--any", "-q", "--query",
                "--open", "--mine", "--blocked", "--since", "-h"]
FLAGS = {"ls": FILTER_FLAGS + ["--sort", "-r", "--reverse", "--json", "--csv", "--md"],
         "board": FILTER_FLAGS + ["--md"], "next": FILTER_FLAGS + ["-n", "--json"],
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
                             --priority --assignee -t TAG -q TEXT --open --mine --blocked --since 3d;
                             ls: --sort FIELD -r, --csv --md --json; board: --md)
  new [TITLE] [flags]        create a ticket (-p -a -t -d --status --attach)
  pull | save                Update / Save work (save -m MSG; --set-upstream)
  migrate                    move tickets to the <id>/main.json5 layout
  ID                         show a ticket; then: edit, comment (-m TEXT -f FILES), title TEXT,
                             assign NAME, tag +A -B, status VALUE, start | done | reopen, priority VALUE,
                             attach FILES, detach SPECS, open [SPECS], deps, blocked IDS, block-by IDS,
                             remove blocked|block-by IDS, rm [-y]
  cd ID                      make a ticket current: the prompt shows it and the actions above work
                             without the ID (`done`, `comment -m hi`, `open 2`); `cd` or `cd ..` goes back
  show [ID]                  show the current (or the given) ticket
  use REPO                   switch to another repo (path, name or number)
  help | h | ?               this text
  exit | quit | q | Ctrl-D   leave
short forms   repo level:  l list=ls  b=board  n=next  i=info  a add=new  pl=pull  sv=save  ..=cd ..
              on a ticket: e=edit  c=comment  st=status  d=done  ro=reopen  pr=priority  o=open
                           at=attach  dt=detach  bl=blocked  bb=block-by  t=title  as=assign
                           tg=tag  dp=deps  del=delete   (a real ticket id always wins over a short form)
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


def complete(engine: Engine, words: list[str], cur: str, registry=None, db_path=None,
             current: str | None = None) -> list[str]:
    """Completions for `cur` given the words already typed on the line. `current` is the
    ticket selected with `cd`: its actions can then be typed without the id."""
    def pick(options) -> list[str]:
        return sorted({o for o in options if o.startswith(cur)})

    ids = ticket_ids(engine)
    lower = {i.lower() for i in ids}
    if current and current in ids:
        if not words:
            return pick(TICKET_FLAGS) if cur.startswith("-") else pick(COMMANDS + ids + TICKET_ACTIONS)
        if words[0] in TICKET_ACTIONS or words[0] in cli.TICKET_ALIASES or words[0].startswith("-"):
            words = [current] + words
    if words and words[0] in cli.REPO_ALIASES and words[0].lower() not in lower:
        words = [cli.REPO_ALIASES[words[0]], *words[1:]]  # `l --st<Tab>` completes like `ls --st<Tab>`
    if len(words) > 1 and words[0].lower() in lower and words[1] in cli.TICKET_ALIASES:
        words = [words[0], cli.TICKET_ALIASES[words[1]], *words[2:]]
    first = words[0] if words else None
    prev = words[-1] if words else None
    if first is None:
        return pick(COMMANDS + ids)
    if first in ("cd", "show"):
        return pick(ids + ([".."] if first == "cd" else [])) if len(words) == 1 else []
    if first == "use":
        return pick(r["name"] for r in repos.known(registry, db_path) if r["exists"])
    if first in FLAGS:  # ls, board, next, new, pull, save, ...
        if prev in ("--status",):
            return pick(STATUSES)
        if prev == "--sort":
            return pick(cli.SORT_FIELDS)
        if prev == "--since":
            return pick(SINCE_HINTS)
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
    if rest and rest[0] in RAW_ACTIONS:  # the words after these are free text, tag changes or -y
        if rest[0] == "tag":
            mine = engine.get_task(match)["tags"]
            known = [t["tag"] for t in engine.all_tags()]
            return pick(["+" + t for t in known] + ["-" + t for t in mine] + ([] if cur[:1] in "+-" and cur else known))
        if rest[0] == "assign":
            return pick(engine.assignees() + ["me", "-"]) if len(rest) == 1 else []
        return pick(["-y", "--yes"]) if rest[0] in ("rm", "delete") else []
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
    current: str | None = None  # the ticket selected with `cd`
    interactive = sys.stdin.isatty() if interactive is None else interactive
    save_history = setup_readline(
        lambda words, cur: complete(engine, words, cur, registry, db_path, current)) if interactive else None
    if interactive:
        print(f"{Path(engine.project).name}: type help for commands, Tab completes, "
              "exit or Ctrl-D to leave")
    try:
        while True:
            name = Path(engine.project).name + (f"/{current}" if current else "")
            try:
                line = input_fn(f"{name}> ")
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
            if words[0] == "..":
                words = ["cd", ".."]
            cmd = words[0]
            if cmd in ("exit", "quit", "q"):
                break
            if cmd in ("help", "h", "?"):
                print(HELP)
                continue
            if cmd == "serve" or (cmd == "open" and not current):
                print(f"{cmd} is not available here (it starts the web server): run it from the shell: "
                      f"{p} {'open' if cmd == 'open' else Path(engine.project).name + ' serve'}"
                      + (" [REPO]" if cmd == "open" else "") + "; in a ticket, open opens its files",
                      file=sys.stderr)
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
                    current = None
                except SystemExit as e:
                    print(e.code if isinstance(e.code, str) else "error: cannot open that repo", file=sys.stderr)
                except EngineError as e:
                    print(f"error: {e}", file=sys.stderr)
                continue
            try:
                engine.refresh()
                if cmd == "cd":
                    if len(words) > 2:
                        print("usage: cd [TICKET | ..]", file=sys.stderr)
                    elif len(words) == 1 or words[1] in ("..", "/"):
                        current = None
                    else:
                        current = cli.find_ticket(engine, words[1])
                    continue
                if cmd == "show":
                    args = words[1:]
                    tid = args.pop(0) if args and not args[0].startswith("-") else current
                    if not tid:
                        print("no ticket selected: cd TKT-101, or show TKT-101", file=sys.stderr)
                        continue
                    words = [tid, *args]
                elif current and (cmd in TICKET_ACTIONS or cmd in cli.TICKET_ALIASES or cmd.startswith("-")) \
                        and not cli.ticket_exists(engine, cmd):
                    words = [current, *words]  # an action on the current ticket
                cli.dispatch(engine, words, p)
            except SystemExit as e:  # fail() and argparse exit; the prompt carries on
                if isinstance(e.code, str):
                    print(e.code, file=sys.stderr)
            except KeyboardInterrupt:
                print()
            except (EngineError, PermissionError, OSError) as e:
                print(f"error: {e}", file=sys.stderr)
            if current:  # it may be gone now (rm, or removed from outside)
                try:
                    engine.refresh()
                    if current not in ticket_ids(engine):
                        print(f"{current} no longer exists; back to the repo", file=sys.stderr)
                        current = None
                except EngineError:
                    pass
    finally:
        if save_history:
            save_history()
