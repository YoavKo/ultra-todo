"""Repo-scoped terminal commands: ``utd REPO [ls | board | serve | ID [edit | comment]]``.

REPO is ``.`` (or any path) or the name / ``ls`` number of a known repo. Everything except
``serve``, ``ID edit`` and ``ID comment`` is read-only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unicodedata
from datetime import datetime
from pathlib import Path

import repos
from engine import (PRIORITIES, STATUSES, Engine, EngineError, NotFound, SyncError,
                    ValidationError, clean_task, fold, load_file, run_git)

# first words that are not a REPO (handled by app.main)
COMMANDS = ("ls", "open", "forget")
STATUS_ALIASES = {"todo": "todo", "to-do": "todo", "open": "todo", "reopen": "todo",
                  "doing": "in_progress", "in_progress": "in_progress", "in-progress": "in_progress",
                  "progress": "in_progress", "wip": "in_progress", "start": "in_progress",
                  "done": "done", "finish": "done", "blocked": "blocked"}
STATUS_SHORTCUTS = {"start": "in_progress", "done": "done", "reopen": "todo"}
STATUS_LABEL = {"todo": "To do", "in_progress": "In progress", "done": "Done", "blocked": "Blocked"}
PRIORITY_MARK = {"high": "▲", "medium": "●", "low": "▼"}
BOARD_ORDER = ("todo", "in_progress", "done", "blocked")
_COLORS = {"red": "31", "green": "32", "yellow": "33", "blue": "34", "dim": "2", "bold": "1"}
STATUS_COLOR = {"todo": None, "in_progress": "blue", "done": "green", "blocked": "red"}
PRIORITY_COLOR = {"high": "red", "medium": "yellow", "low": "dim"}


# ---- terminal helpers ------------------------------------------------------
def use_color() -> bool:
    if "NO_COLOR" in os.environ:
        return False
    return sys.stdout.isatty() or os.environ.get("CLICOLOR_FORCE", "0") not in ("", "0")


def paint(text: str, color: str | None) -> str:
    return f"\x1b[{_COLORS[color]}m{text}\x1b[0m" if color and use_color() else text


def term_width() -> int:
    return shutil.get_terminal_size((100, 24)).columns


def dw(s: str) -> int:
    """Display width: wide CJK = 2, combining marks (e.g. niqqud) = 0."""
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1
               for c in s)


def clip(s: str, width: int) -> str:
    if dw(s) <= width:
        return s
    out, used = "", 0
    for c in s:
        w = dw(c)
        if used + w > width - 1:
            break
        out, used = out + c, used + w
    return out + "…"


def pad(s: str, width: int) -> str:
    return s + " " * max(0, width - dw(s))


def wrap(s: str, width: int, max_lines: int) -> list[str]:
    lines = textwrap.wrap(s, width=width, break_long_words=True) or [""]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = clip(lines[-1] + "…", width)
    return lines


def local_time(iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def fail(msg: str):
    raise SystemExit(f"error: {msg}")


# ---- repo / ticket lookup --------------------------------------------------
def looks_like_path(spec: str) -> bool:
    return spec in (".", "..") or spec.startswith(("/", "~", "./", "../")) or os.sep in spec


def resolve_repo(spec: str, db_path=None, registry=None) -> str:
    if looks_like_path(spec):
        path = Path(spec).expanduser()
        if not path.is_dir():
            raise EngineError(f"not a directory: {spec}")
        return str(path)
    try:
        return repos.resolve(spec, registry, db_path)
    except EngineError as e:
        if "no known repo" in str(e):
            raise EngineError(f"{e}; for a directory write ./{spec}")
        raise


def find_ticket(engine: Engine, spec: str) -> str:
    """Exact id, else case-insensitive, else a bare number matching one ``PREFIX-N``."""
    ids = [r[0] for r in engine.db.execute("SELECT id FROM tasks WHERE project_path=?",
                                           (engine.project,))]
    if spec in ids:
        return spec
    hits = [i for i in ids if i.lower() == spec.lower()]
    if not hits and spec.isdigit():
        hits = [i for i in ids if re.search(rf"[-_]0*{int(spec)}$", i)]
    if len(hits) == 1:
        return hits[0]
    if hits:
        fail(f"{spec!r} matches several tickets: {', '.join(sorted(hits))}")
    fail(f"no ticket {spec!r} in {Path(engine.project).name}"
         + (f" (e.g. {sorted(ids)[0]})" if ids else " (it has no tickets yet)"))


def open_engine(path: str, db_path=None) -> Engine:
    try:
        engine = Engine(path, db_path=db_path)
        engine.refresh()
    except EngineError as e:
        fail(str(e))
    return engine


# ---- formatting ------------------------------------------------------------
def status_text(s: str) -> str:
    return paint(STATUS_LABEL.get(s, s), STATUS_COLOR.get(s))


def prio_text(p: str, word: bool = True) -> str:
    return paint(f"{PRIORITY_MARK.get(p, '?')} {p}" if word else PRIORITY_MARK.get(p, "?"),
                 PRIORITY_COLOR.get(p))


def table(headers: list[str], rows: list[list[str]], plain: list[list[str]], width: int) -> str:
    """Left-aligned table. `rows` may contain ANSI; `plain` are the same cells without it
    (used for widths). The last column is clipped so lines fit `width`."""
    cols = len(headers)
    widths = [max(dw(headers[c]), *(dw(r[c]) for r in plain)) for c in range(cols)]
    out = []
    for cells, raw in [(headers, headers)] + list(zip(rows, plain)):
        parts = []
        for c in range(cols):
            text = cells[c]
            if c == cols - 1:
                avail = max(8, width - sum(widths[:c]) - 2 * c)
                if dw(raw[c]) > avail:
                    text = clip(raw[c], avail)
            parts.append(text + " " * (widths[c] - dw(raw[c])) if c < cols - 1 else text)
        out.append("  ".join(parts).rstrip())
    return "\n".join(out)


def ticket_row(t: dict) -> tuple[list[str], list[str]]:
    plain_status, plain_prio = STATUS_LABEL[t["status"]], f"{PRIORITY_MARK[t['priority']]} {t['priority']}"
    blocked = ",".join(t["blocked_by"]) or "-"
    tags = " ".join("#" + x for x in t["tags"]) or "-"
    plain = [t["id"], plain_status, plain_prio, t["assignee"] or "-", blocked, tags, t["title"]]
    shown = [t["id"], status_text(t["status"]), prio_text(t["priority"]), plain[3], blocked, tags, t["title"]]
    return shown, plain


def ticket_table(tasks: list[dict]) -> str:
    headers = ["ID", "STATUS", "PRIORITY", "ASSIGNEE", "BLOCKED BY", "TAGS", "TITLE"]
    pairs = [ticket_row(t) for t in tasks]
    return table(headers, [p[0] for p in pairs], [p[1] for p in pairs], term_width())


def card(t: dict, width: int) -> list[str]:
    """A boxed ticket card exactly `width` columns wide (ANSI-safe)."""
    inner = width - 4
    extra = (f" →{len(t['blocks'])}" if t["blocks"] else "") + (f" ✖{len(t['blocked_by'])}" if t["blocked_by"] else "")
    head = f"{t['id']} {PRIORITY_MARK[t['priority']]}{extra}"
    who = " ".join(filter(None, ["@" + t["assignee"] if t["assignee"] else "", *("#" + x for x in t["tags"])]))
    body = [(paint(pad(clip(head, inner), inner), PRIORITY_COLOR.get(t["priority"])), True)]
    body += [(pad(line, inner), False) for line in wrap(t["title"], inner, 3)]
    if who:
        body.append((paint(pad(clip(who, inner), inner), "dim"), True))
    top, bottom = "┌" + "─" * (width - 2) + "┐", "└" + "─" * (width - 2) + "┘"
    return [top] + [f"│ {text} │" for text, _ in body] + [bottom]


def board_text(tasks: list[dict], width: int) -> str:
    by = {s: [t for t in tasks if t["status"] == s] for s in BOARD_ORDER}
    gap = 2
    colw = (width - gap * (len(BOARD_ORDER) - 1)) // len(BOARD_ORDER)
    if colw < 24:  # too narrow for side-by-side: one column after another
        out = []
        for s in BOARD_ORDER:
            out.append(paint(f"{STATUS_LABEL[s]} ({len(by[s])})", "bold"))
            for t in by[s]:
                out.extend(card(t, min(width, 60)))
            out.append("")
        return "\n".join(out).rstrip()
    cols = []
    for s in BOARD_ORDER:
        title = f"{STATUS_LABEL[s]} ({len(by[s])})"
        lines = [paint(pad(title, colw), STATUS_COLOR[s] or "bold"), "─" * colw]
        for t in by[s]:
            lines.extend(card(t, colw))
        cols.append(lines)
    height = max(len(c) for c in cols)
    rows = []
    for i in range(height):
        rows.append((" " * gap).join(c[i] if i < len(c) else " " * colw for c in cols).rstrip())
    return "\n".join(rows)


# ---- commands --------------------------------------------------------------
def add_filters(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--status", choices=STATUSES)
    ap.add_argument("--priority", choices=PRIORITIES)
    ap.add_argument("--assignee", metavar="NAME")
    ap.add_argument("-t", "--tag", action="append", default=[], metavar="TAG",
                    help="only tickets with this tag; repeat for several (all must match)")
    ap.add_argument("--any", action="store_true", help="with several --tag: match any, not all")
    ap.add_argument("-q", "--query", metavar="TEXT", help="search titles, descriptions, comments")
    ap.add_argument("--open", action="store_true", help="hide done tickets")
    ap.add_argument("--mine", action="store_true",
                    help="only tickets assigned to you (git config user.name)")


def me(engine: Engine) -> set[str]:
    name = run_git(engine.root, "config", "user.name", check=False).stdout.strip()
    email = run_git(engine.root, "config", "user.email", check=False).stdout.strip()
    return {fold(x) for x in (name, email.split("@")[0]) if x}


def public(t: dict) -> dict:
    """A task for --json: without the index's internal project path."""
    return {k: v for k, v in t.items() if k != "project_path"}


def filtered(engine: Engine, a: argparse.Namespace) -> list[dict]:
    tasks = engine.list_tasks(status=a.status, assignee=a.assignee, q=a.query, tags=a.tag,
                              priority=a.priority, match="any" if a.any else "all")
    if a.open:
        tasks = [t for t in tasks if t["status"] != "done"]
    if a.mine:
        mine = me(engine)
        tasks = [t for t in tasks if fold(t["assignee"]) in mine]
    return tasks


def cmd_migrate(engine: Engine, argv: list[str], p: str) -> None:
    ap = argparse.ArgumentParser(
        prog=f"{p} REPO migrate",
        description="Move tickets from the old layout (.todo/tasks/ID.json5) to the current one\n"
                    "(.todo/tasks/ID/main.json5, next to its comments and attachments). Uses git mv,\n"
                    "so history follows the file. Nothing is committed: review, then save.")
    ap.parse_args(argv)
    res = engine.migrate_layout()
    if not any(res.values()):
        print("nothing to migrate")
    if res["moved"]:
        print(f"moved {len(res['moved'])} ticket(s) to <id>/main.json5")
    if res["duplicates"]:
        print(f"removed {len(res['duplicates'])} old file(s) identical to the new ones: "
              + ", ".join(res["duplicates"]))
    if res["conflicts"]:
        fail("both layouts exist with different contents, left untouched: "
             + ", ".join(res["conflicts"]) + "\ncompare <id>.json5 with <id>/main.json5 and delete one")
    if res["moved"] or res["duplicates"]:
        print(f"not committed yet: run `{p} REPO save` (or commit with git)")


def cmd_info(engine: Engine, argv: list[str], p: str) -> None:
    ap = argparse.ArgumentParser(prog=f"{p} REPO", description="Project info: branch, sync state "
                                 "and ticket counts.")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    tasks, sync = engine.list_tasks(), engine.sync_status()
    count = lambda key, val: sum(1 for t in tasks if t[key] == val)
    openn = [t for t in tasks if t["status"] != "done"]
    people: dict[str, int] = {}
    for t in openn:
        if t["assignee"]:
            people[t["assignee"]] = people.get(t["assignee"], 0) + 1
    legacy = engine.legacy_tasks()
    info = {"name": Path(engine.project).name, "path": engine.project, **sync,
            "legacy_layout_tickets": len(legacy),
            "tickets": len(tasks), "by_status": {s: count("status", s) for s in STATUSES},
            "open_by_priority": {q: sum(1 for t in openn if t["priority"] == q) for q in PRIORITIES},
            "open_by_assignee": people, "tags": {x["tag"]: x["count"] for x in engine.all_tags()}}
    if a.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return
    branch = info["branch"] + (f" → {info['upstream']}" if info["upstream"] else "  (no upstream)")
    if info["ahead"] or info["behind"]:
        branch += f"  (ahead {info['ahead']}, behind {info['behind']})"
    join = lambda d: "  ·  ".join(f"{k} {v}" for k, v in d.items() if v) or "-"
    print(paint(info["name"], "bold") + "  " + info["path"])
    print(f"branch:     {branch}")
    print(f"uncommitted .todo changes: {info['changes']}")
    print(f"tickets:    {info['tickets']}  ·  " + "  ·  ".join(
        f"{STATUS_LABEL[s].lower()} {n}" for s, n in info["by_status"].items()))
    print(f"open by priority:  {join(info['open_by_priority'])}")
    print(f"open by assignee:  {join(people)}")
    print(f"tags:       {join(info['tags'])}")
    if legacy:
        print(paint(f"{len(legacy)} ticket(s) use the old flat file layout; "
                    f"run: {p} REPO migrate", "yellow"))


def cmd_ls(engine: Engine, argv: list[str], p: str) -> None:
    ap = argparse.ArgumentParser(
        prog=f"{p} REPO ls", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="List the tickets of a repo as a table (high priority first).",
        epilog="examples:\n"
               f"  • {p} . ls --status todo --priority high\n"
               f"  • {p} my-repo ls -t backend -t auth        tickets with both tags\n"
               f"  • {p} 2 ls -q login --json")
    add_filters(ap)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--ids", action="store_true", help=argparse.SUPPRESS)  # tab completion
    a = ap.parse_args(argv)
    tasks = filtered(engine, a)
    if a.ids:
        print("\n".join(t["id"] for t in tasks))
    elif a.json:
        print(json.dumps([public(t) for t in tasks], ensure_ascii=False, indent=2))
    elif not tasks:
        print("no tickets")
    else:
        print(ticket_table(tasks))
        print(paint(f"{len(tasks)} ticket{'s' if len(tasks) != 1 else ''}", "dim"))


def cmd_board(engine: Engine, argv: list[str], p: str) -> None:
    ap = argparse.ArgumentParser(
        prog=f"{p} REPO board", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="The kanban board in the terminal: To do / In progress / Done / Blocked.\n"
                    "Fits the terminal width; on a narrow terminal the columns stack.",
        epilog=f"cards show: ID, priority (▲ high ● medium ▼ low), →N tickets it blocks,\n"
               f"✖N tickets blocking it, @assignee and #tags.")
    add_filters(ap)
    a = ap.parse_args(argv)
    print(board_text(filtered(engine, a), term_width()))


def render_ticket(engine: Engine, t: dict) -> str:
    by_id = {x["id"]: x for x in engine.list_tasks()}
    ref = lambda i: f"{i}  {by_id[i]['title']}  [{STATUS_LABEL[by_id[i]['status']]}]" if i in by_id else f"{i}  (missing)"
    out = [f"{paint(t['id'], 'bold')}  {t['title']}",
           f"status: {status_text(t['status'])}   priority: {prio_text(t['priority'])}"
           f"   assignee: {t['assignee'] or '-'}",
           f"tags: {' '.join('#' + x for x in t['tags']) or '-'}"]
    if t["blocked_by"]:
        out.append("blocked by:\n" + "\n".join("  " + ref(i) for i in t["blocked_by"]))
    if t["blocks"]:
        out.append("blocks:\n" + "\n".join("  " + ref(i) for i in t["blocks"]))
    out.append(paint("file: " + engine.task_path(t["id"]).relative_to(engine.root).as_posix(), "dim"))
    if t["description"].strip():
        out += ["", t["description"].rstrip()]
    if t["attachments"]:
        out += ["", paint(f"Attachments ({len(t['attachments'])})", "bold")]
        for a in t["attachments"]:
            where = engine.task_dir(t["id"]) / a["file"]
            out.append(f"  {a['name']}  ({a['size'] / 1024:.1f} KB)  "
                       + paint(where.relative_to(engine.root).as_posix(), "dim"))
    comments = engine.get_comments(t["id"])
    out += ["", paint(f"Comments ({len(comments)})", "bold")]
    for c in comments:
        edited = " (edited)" if c["edited_at"] else ""
        out.append(paint(f"  {c['author']} · {local_time(c['created_at'])}{edited}", "dim"))
        out += ["    " + line for line in c["body"].splitlines()] + [""]
    return "\n".join(out).rstrip()


def editor_command() -> list[str]:
    cmd = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "")
    if not cmd:
        found = next((e for e in ("nano", "vim", "vi") if shutil.which(e)), None)
        if not found:
            fail("no editor found; set $EDITOR (e.g. export EDITOR=nano)")
        cmd = [found]
    return cmd


def run_editor(path: Path) -> None:
    try:
        rc = subprocess.run([*editor_command(), str(path)]).returncode
    except FileNotFoundError:
        fail(f"editor not found: {editor_command()[0]}")
    if rc != 0:
        fail(f"the editor exited with status {rc}")


def cmd_edit(engine: Engine, tid: str, p: str) -> None:
    path = engine.task_path(tid)
    before = path.read_bytes()
    run_editor(path)
    if path.read_bytes() == before:
        print(f"{tid}: no changes")
        return
    try:
        task = clean_task(load_file(path), tid)
        if load_file(path).get("id") not in (None, tid):
            raise ValidationError(f"the id must stay {tid}")
        engine.refresh()
        engine._check_graph(task)
    except Exception as e:  # JSON5 syntax errors come from the parser, not EngineError
        fail(f"{tid} was saved but is not valid: {e}\n"
             f"fix it with `{p} REPO {tid} edit`, or undo with: git checkout -- {path.relative_to(engine.root)}")
    print(f"{tid}: updated (not committed; use save or git)")


COMMENT_HELP = "# Write your comment above. Lines starting with '#' are ignored.\n# An empty comment aborts.\n"


def cmd_comment(engine: Engine, tid: str, message: str | None) -> None:
    if message is None and not sys.stdin.isatty():
        message = sys.stdin.read()
    if message is None:
        with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write("\n\n" + COMMENT_HELP)
        tmp = Path(f.name)
        try:
            run_editor(tmp)
            message = "\n".join(line for line in tmp.read_text(encoding="utf-8").splitlines()
                                if not line.startswith("#"))
        finally:
            tmp.unlink(missing_ok=True)
    try:
        c, _ = engine.add_comment(tid, message)
    except ValidationError:
        fail("empty comment, nothing added")
    print(f"commented on {tid} as {c['author']}  ({c['id']})")


def parse_status(value: str, ap: argparse.ArgumentParser) -> str:
    key = value.strip().lower().replace(" ", "_")
    if key not in STATUS_ALIASES:
        ap.error(f"invalid status {value!r} (choose todo, doing, done or blocked)")
    return STATUS_ALIASES[key]


def cmd_status(engine: Engine, tid: str, value: str, ap: argparse.ArgumentParser) -> None:
    new = parse_status(value, ap)
    old = engine.get_task(tid)["status"]
    if old == new:
        print(f"{tid}: already {STATUS_LABEL[new].lower()}")
        return
    engine.set_status(tid, new)
    print(f"{tid}: {STATUS_LABEL[old].lower()} → {STATUS_LABEL[new].lower()}")


PRIORITY_ALIASES = {"h": "high", "m": "medium", "med": "medium", "l": "low"}


def cmd_priority(engine: Engine, tid: str, value: str, ap: argparse.ArgumentParser) -> None:
    value = PRIORITY_ALIASES.get(value.lower(), value.lower())
    if value not in PRIORITIES:
        ap.error(f"invalid priority {value!r} (choose high, medium or low)")
    old = engine.get_task(tid)["priority"]
    if old == value:
        print(f"{tid}: priority is already {value}")
        return
    engine.save_task({"id": tid, "priority": value})
    print(f"{tid}: priority {old} → {value}")


def read_files(paths: list[str]) -> list[tuple[str, bytes]]:
    files = []
    for p in paths:
        path = Path(p).expanduser()
        if not path.is_file():
            fail(f"not a file: {p}")
        files.append((path.name, path.read_bytes()))
    return files


def cmd_attach(engine: Engine, tid: str, paths: list[str]) -> None:
    files = read_files(paths)
    try:
        task, _ = engine.save_task({"id": tid}, files=files)
    except ValidationError as e:
        fail(str(e))
    names = [a["name"] for a in task["attachments"]][-len(files):]
    print(f"{tid}: attached {', '.join(names)}")


def cmd_detach(engine: Engine, tid: str, names: list[str]) -> None:
    have = {a["name"] for a in engine.get_task(tid)["attachments"]}
    missing = [n for n in names if n not in have]
    if missing:
        fail(f"{tid} has no attachment {', '.join(map(repr, missing))}"
             + (f" (it has: {', '.join(sorted(have))})" if have else " (it has none)"))
    engine.save_task({"id": tid}, remove_files=names)
    print(f"{tid}: removed {', '.join(names)}")


DEP_WORDS = ("blocked", "block-by")


def cmd_deps(engine: Engine, tid: str, kind: str, remove: bool, specs: list[str]) -> None:
    """kind 'blocked': TID blocks each spec; kind 'block-by': TID is blocked by each spec."""
    for spec in specs:
        current = engine.get_task(tid)["blocked_by"]
        other = spec if remove and kind == "block-by" and spec in current else find_ticket(engine, spec)
        task, blocker = (other, tid) if kind == "blocked" else (tid, other)
        try:
            (engine.remove_dependency if remove else engine.add_dependency)(task, blocker)
        except NotFound as e:
            fail(f"no ticket {e}")
        except ValidationError as e:
            fail(f"cannot make {task} blocked by {blocker}: {e}"
                 + (" (a dependency cycle)" if "->" in str(e) else ""))
        verb = "no longer" if remove else "now"
        print(f"{task} is {verb} blocked by {blocker}")


def cmd_ticket(engine: Engine, spec: str, argv: list[str], p: str) -> None:
    ap = argparse.ArgumentParser(
        prog=f"{p} REPO ID", formatter_class=argparse.RawDescriptionHelpFormatter,
        usage=f"{p} REPO ID [edit | comment | status VALUE | start | done | reopen | priority VALUE | "
              "attach FILES | detach NAMES | blocked IDS | block-by IDS | remove blocked IDS | "
              "remove block-by IDS] [flags]",
        description="Show a ticket, edit it, comment on it, set its priority, or change its dependencies.",
        epilog="examples:\n"
               f"  • {p} . TKT-101                 show the ticket (a bare 101 works too)\n"
               f"  • {p} . TKT-101 edit            open the ticket file in $VISUAL / $EDITOR\n"
               f"  • {p} . TKT-101 comment         write a comment in $EDITOR\n"
               f"  • {p} . TKT-101 -c -m 'done'    add a comment from the command line\n"
               f"  • echo 'looks good' | {p} . TKT-101 comment\n"
               f"  • {p} . TKT-101 status doing    todo | doing | done | blocked (-s done also works)\n"
               f"  • {p} . TKT-101 done            shortcuts: start (doing), done, reopen (todo)\n"
               f"  • {p} . TKT-101 priority high   set the priority (high | medium | low; -p h also works)\n"
               f"  • {p} . TKT-101 attach shot.png log.txt   attach files to the description (-a)\n"
               f"  • {p} . TKT-101 detach shot.png           remove an attachment (-d)\n"
               f"  • {p} . TKT-101 blocked 104     TKT-101 blocks TKT-104\n"
               f"  • {p} . TKT-104 block-by 101    TKT-104 is blocked by TKT-101 (same link)\n"
               f"  • {p} . TKT-101 remove blocked 104       remove that link\n"
               f"  • {p} . TKT-104 -rbb 101                 ...from the other side\n"
               "\nattachments live in <id>/attachments/description/ next to main.json5.\n"
               "priority flag: -p/--priority VALUE (h, m, l and med are accepted too).\n"
               "dependency flags: -b/--blocked, -bb/--block-by, -rb/--remove-blocked,\n"
               "-rbb/--remove-block-by (each takes one or more ticket ids). Cycles are refused.\n"
               "edit and the dependency commands change files under .todo/ (and comment adds one);\n"
               "none of them commit: use Save work in the app or git.")
    ap.add_argument("words", nargs="*", metavar="ACTION",
                    help="edit | comment | blocked IDS | block-by IDS | remove blocked|block-by IDS")
    ap.add_argument("-e", "--edit", action="store_true", help="same as the edit action")
    ap.add_argument("-c", "--comment", action="store_true", help="same as the comment action")
    ap.add_argument("-m", "--message", help="comment text (with comment)")
    ap.add_argument("-p", "--priority", metavar="VALUE", help="set the priority: high | medium | low")
    ap.add_argument("-s", "--status", metavar="VALUE", help="set the status: todo | doing | done | blocked")
    ap.add_argument("-a", "--attach", nargs="+", metavar="FILE", help="attach files to the description")
    ap.add_argument("-d", "--detach", nargs="+", metavar="NAME", help="remove description attachments")
    for flag, dest in (("-b", "blocked"), ("-bb", "block_by"),
                       ("-rb", "remove_blocked"), ("-rbb", "remove_block_by")):
        ap.add_argument(flag, "--" + dest.replace("_", "-"), dest=dest, nargs="+", metavar="ID")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    ops: list[tuple] = []  # (name, kind, remove, ids)
    w = a.words
    if w:
        head, ids = w[0], w[1:]
        if head == "remove":
            if not ids or ids[0] not in DEP_WORDS:
                ap.error("remove needs blocked or block-by, e.g. remove block-by 101")
            ops.append(("dep", ids[0], True, ids[1:]))
        elif head in DEP_WORDS:
            ops.append(("dep", head, False, ids))
        elif head == "status" or head in STATUS_SHORTCUTS:
            value = STATUS_SHORTCUTS.get(head) or (ids[0] if len(ids) == 1 else None)
            if value is None or (head in STATUS_SHORTCUTS and ids):
                ap.error("status needs one value: todo, doing, done or blocked"
                         if head == "status" else f"{head} takes no arguments")
            ops.append(("status", value))
        elif head in ("attach", "detach"):
            if not ids:
                ap.error(f"{head} needs at least one " + ("file" if head == "attach" else "attachment name"))
            ops.append((head, ids))
        elif head == "priority":
            if len(ids) != 1:
                ap.error("priority needs one value: high, medium or low")
            ops.append(("priority", ids[0]))
        elif head in ("edit", "comment") and not ids:
            ops.append((head,))
        else:
            ap.error(f"unknown action {' '.join(w)!r}")
    ops += [(n,) for n, on in (("edit", a.edit), ("comment", a.comment)) if on]
    if a.priority is not None:
        ops.append(("priority", a.priority))
    if a.status is not None:
        ops.append(("status", a.status))
    if a.attach:
        ops.append(("attach", a.attach))
    if a.detach:
        ops.append(("detach", a.detach))
    ops += [("dep", k, rm, ids) for k, rm, ids in (
        ("blocked", False, a.blocked), ("block-by", False, a.block_by),
        ("blocked", True, a.remove_blocked), ("block-by", True, a.remove_block_by)) if ids]
    if len(ops) > 1 and {o[0] for o in ops} != {"dep"}:
        ap.error("choose one of edit, comment, status, priority, attach, detach or dependency changes")
    if any(o[0] == "dep" and not o[3] for o in ops):
        ap.error("give at least one ticket id")
    if a.message is not None and not any(o[0] == "comment" for o in ops):
        ap.error("-m/--message needs comment (-c)")
    tid = find_ticket(engine, spec)
    if not ops:
        t = engine.get_task(tid)
        if a.json:
            print(json.dumps({**public(t), "comments": engine.get_comments(tid)}, ensure_ascii=False, indent=2))
        else:
            print(render_ticket(engine, t))
    elif ops[0][0] == "edit":
        cmd_edit(engine, tid, p)
    elif ops[0][0] == "comment":
        cmd_comment(engine, tid, a.message)
    elif ops[0][0] == "priority":
        cmd_priority(engine, tid, ops[0][1], ap)
    elif ops[0][0] == "status":
        cmd_status(engine, tid, ops[0][1], ap)
    elif ops[0][0] == "attach":
        cmd_attach(engine, tid, ops[0][1])
    elif ops[0][0] == "detach":
        cmd_detach(engine, tid, ops[0][1])
    else:
        for _, kind, remove, ids in ops:
            cmd_deps(engine, tid, kind, remove, ids)


def cmd_new(engine: Engine, argv: list[str], p: str) -> None:
    ap = argparse.ArgumentParser(
        prog=f"{p} REPO new", formatter_class=argparse.RawDescriptionHelpFormatter,
        usage=f"{p} REPO new [TITLE] [flags]",
        description="Create a ticket and print its id. Without a title, $EDITOR opens: the first\n"
                    "line is the title, the rest the description (lines starting with # are ignored).",
        epilog="examples:\n"
               f"  • {p} . new 'Fix login redirect' -p high -a alice -t backend -t auth\n"
               f"  • {p} . new 'Write docs' -d 'Cover the public API' --status doing\n"
               "\nThe file is written, not committed: use save (or Save work in the app).")
    ap.add_argument("title", nargs="*")
    ap.add_argument("-p", "--priority", default="medium", metavar="VALUE")
    ap.add_argument("-a", "--assignee", default="", metavar="NAME")
    ap.add_argument("-t", "--tag", action="append", default=[], metavar="TAG",
                    help="repeat for several, or comma-separated")
    ap.add_argument("-d", "--description", default="")
    ap.add_argument("--status", default="todo", metavar="VALUE")
    ap.add_argument("--id", help="ticket id (default: next TKT-N)")
    ap.add_argument("--attach", nargs="+", default=[], metavar="FILE",
                    help="attach files to the description")
    a = ap.parse_args(argv)
    files = read_files(a.attach)
    title, description = " ".join(a.title).strip(), a.description
    if not title:
        with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write("\n\n# First line: the title. The rest: the description.\n"
                    "# Lines starting with '#' are ignored. An empty title aborts.\n")
        tmp = Path(f.name)
        try:
            run_editor(tmp)
            lines = [l for l in tmp.read_text(encoding="utf-8").splitlines() if not l.startswith("#")]
        finally:
            tmp.unlink(missing_ok=True)
        text = "\n".join(lines).strip()
        title, _, description = text.partition("\n")
        title, description = title.strip(), description.strip()
    if not title:
        fail("empty title, nothing created")
    priority = PRIORITY_ALIASES.get(a.priority.lower(), a.priority.lower())
    if priority not in PRIORITIES:
        ap.error(f"invalid priority {a.priority!r} (choose high, medium or low)")
    data = {"title": title, "priority": priority, "assignee": a.assignee, "description": description,
            "status": parse_status(a.status, ap),
            "tags": [x for t in a.tag for x in t.split(",")]}
    if a.id:
        data["id"] = a.id
    try:
        task, _ = engine.save_task(data, create=True, files=files)
    except ValidationError as e:
        fail(str(e))
    print(f"created {task['id']}  {task['title']}")


def cmd_next(engine: Engine, argv: list[str], p: str) -> None:
    ap = argparse.ArgumentParser(
        prog=f"{p} REPO next", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="What to work on: open tickets that nothing unfinished blocks, in progress\n"
                    "first within each priority, highest priority first.",
        epilog=f"example: {p} . next --mine -n 3")
    add_filters(ap)
    ap.add_argument("-n", type=int, metavar="N", help="show only the first N")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    done = {t["id"] for t in engine.list_tasks(status="done")}
    ready = [t for t in filtered(engine, a)
             if t["status"] in ("todo", "in_progress") and all(b in done for b in t["blocked_by"])]
    ready.sort(key=lambda t: (PRIORITIES.index(t["priority"]), t["status"] != "in_progress", t["id"]))
    ready = ready[:a.n] if a.n else ready
    if a.json:
        print(json.dumps([public(t) for t in ready], ensure_ascii=False, indent=2))
    elif not ready:
        print("nothing ready: no open ticket without unfinished blockers")
    else:
        print(ticket_table(ready))


SYNC_MESSAGES = {
    "no_upstream": "this branch has no upstream; publish it with: {p} REPO save --set-upstream",
    "no_remote": "this repository has no remote configured",
    "diverged": "your branch and the remote have diverged; update with: {p} REPO pull --rebase",
    "conflict": "update stopped: the same files changed on both sides; nothing was changed. "
                "Resolve these files in git:",
    "stash_conflict": "updated, but re-applying your local edits conflicted; they are kept in "
                      "git stash. Files:",
    "rejected": "the remote has newer changes; run {p} REPO pull first, then save again",
    "push_failed": "push failed",
    "failed": "update failed",
}


def sync_fail(e: SyncError, p: str):
    msg = SYNC_MESSAGES.get(e.code, e.code).format(p=p)
    detail = e.detail if e.detail and e.detail != e.code else ""
    fail("\n".join(filter(None, [msg, *("  " + f for f in e.files), detail])))


def cmd_pull(engine: Engine, argv: list[str], p: str) -> None:
    ap = argparse.ArgumentParser(
        prog=f"{p} REPO pull", description="The app's Update button: git pull --ff-only, then refresh "
        "the index. Rebase is offered only if the branch diverged.")
    ap.add_argument("--rebase", action="store_true", help="rebase local commits if the branch diverged")
    a = ap.parse_args(argv)
    try:
        s = engine.pull(rebase=a.rebase)
    except SyncError as e:
        sync_fail(e, p)
    if s["full"]:
        print("updated; index rebuilt")
    elif not s.changed:
        print("already up to date")
    else:
        print(f"updated: {s['tasks_added']} added, {s['tasks_updated']} changed, "
              f"{s['tasks_deleted']} removed, {s['comments_changed']} comments")


def cmd_save(engine: Engine, argv: list[str], p: str) -> None:
    ap = argparse.ArgumentParser(
        prog=f"{p} REPO save", description="The app's Save work button: commit only the changes "
        "under .todo/, then push. Never force-pushes, never stages other files.")
    ap.add_argument("-m", "--message", help="commit message (default: tasks: update N task(s))")
    ap.add_argument("--set-upstream", action="store_true",
                    help="publish the branch (git push -u) if it has no upstream")
    a = ap.parse_args(argv)
    try:
        res = engine.save_work(a.message, a.set_upstream)
    except SyncError as e:
        sync_fail(e, p)
    print(f"committed: {res['message']}" if res["committed"] else "nothing to commit")
    if res["pushed"]:
        print("pushed")


def run(argv: list[str], p: str, db_path=None, registry=None) -> list[str] | None:
    """Handle ``REPO [ACTION ...]``. For ``serve`` return the argv to serve with (the caller
    starts the server); otherwise do the work and return None."""
    spec, rest = argv[0], argv[1:]
    if spec in ("-h", "--help"):
        return None
    try:
        path = resolve_repo(spec, db_path, registry)
    except EngineError as e:
        fail(str(e))
    action = rest[0] if rest else None
    if action == "serve":
        return ["--target", path, *rest[1:]]
    if action in ("-h", "--help"):
        print(f"usage: {p} REPO [ls | board | next | new | pull | save | migrate | serve | ID [action]]\n"
              f"see {p} -h for the commands, or {p} REPO ls -h / board -h / ID -h")
        return None
    engine = open_engine(path, db_path)
    if action is None or action.startswith("-"):
        cmd_info(engine, rest, p)
    elif action == "ls":
        cmd_ls(engine, rest[1:], p)
    elif action == "board":
        cmd_board(engine, rest[1:], p)
    elif action in ("new", "next", "pull", "save", "migrate"):
        {"new": cmd_new, "next": cmd_next, "pull": cmd_pull, "save": cmd_save,
         "migrate": cmd_migrate}[action](
            engine, rest[1:], p)
    else:
        cmd_ticket(engine, action, rest[1:], p)
    return None
