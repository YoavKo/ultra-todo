# ultra-todo

A self-hosted, local-first task manager written in Python (Flask). It manages **any local Git repository** that contains a `.todo/tasks/` directory of plain-text JSON5 files. Hebrew and English are both supported (UI, RTL layout, and content).

> Status: v0.1 – working. Design: [PLAN.md](PLAN.md) (English) and [plan_he.html](plan_he.html) (full plan in Hebrew).

## Core ideas

- **Zero-conflict GitOps** – the managed repo holds only text (`.json5`) written by the app. No database ever touches your project.
- **Smart persistent index** – a single SQLite file in the global cache dir (`~/.cache/ultra-todo/app_global_index.sqlite3`, or the OS equivalent) serves fast lookups for all projects.
- **Git hash invalidation** – on boot the app compares `git rev-parse HEAD` (plus uncommitted changes under `.todo/`) with the cached value. Unchanged → no disk scan; changed → only the modified files are re-parsed.
- **DAG dependencies** – "blocked by" links are checked with DFS cycle detection; cycles return HTTP 400.

## Features

- Kanban board and list view
- **Priority** – high / medium / low (default medium): coloured badge on cards, priority filter, high-priority tasks listed first
- **Dependencies** – each task page shows what blocks it and what it blocks, with one-click × to remove a link and a picker to add one (cycles are rejected)
- **Tags** on tasks, with filter chips (AND / OR, shareable via URL)
- **Comments** per task: newest first, author and time stamped by the server, editable, with image/file attachments stored next to the task
- **Update** button: `git pull --ff-only`, then the index is refreshed from the diff (rebase offered if the branch diverged; aborted cleanly on conflict)
- **Save work** button: commits only `.todo/` changes (editable message) and pushes; never force-pushes, never stages files outside `.todo/`
- Hebrew + English: translated UI, RTL layout, `dir="auto"` for mixed-language content; language set with `--lang` and/or the in-app toggle

## Install and run (uv)

```bash
uv sync
uv run ultra-todo --target /path/to/your/repo            # http://127.0.0.1:8080
uv run ultra-todo --target /path/to/repo --lang he --port 9000 --auto-commit
```

Global install: `uv tool install .` then `ultra-todo --target ...`.
Without uv: `pip install -r requirements.txt && python src/app.py --target ...`.

### Shell shortcut

Add this to `~/.bashrc` (adjust the checkout path) to run `utd [path] [flags]` from anywhere;
the path defaults to the current directory and extra flags are passed through:

```bash
# ultra-todo: utd [path] [extra flags]  (path defaults to .)
utd() {
    local target=.
    if [ $# -gt 0 ] && [[ "$1" != -* ]]; then target="$1"; shift; fi
    target="$(realpath -- "$target")" || return 1
    uv run --project "$HOME/path/to/ultra-todo" ultra-todo --target "$target" "$@"
}
```

```bash
utd                          # current directory
utd --lang he                # flags only: still the current directory
utd ~/work/some-repo --lang he --port 9000
```

### CLI flags

| Flag | Meaning |
|---|---|
| `--target PATH` | Git repo to manage (must contain or get `.todo/tasks/`) |
| `--lang he\|en` | Default UI language (users can override in the UI) |
| `--port N` | Port (default 8080), bound to 127.0.0.1 |
| `--auto-commit` | Commit each change as `task: update <ID>` |
| `--author NAME` | Override the comment author (default: `git config user.name`) |
| `--max-attachment-mb N` | Attachment size limit (default 10) |

## File formats

```
<repo>/.todo/tasks/
├── TKT-101.json5
└── TKT-101/
    ├── comments/20261018T100000123Z-ab12cd.json5
    └── attachments/<comment-id>/screenshot.png
```

Task:

```json5
{
  id: "TKT-101",
  title: "תיקון באג בהתחברות",
  status: "in_progress",        // todo | in_progress | done | blocked
  priority: "high",             // high | medium | low (optional, default medium)
  assignee: "yoav",
  description: "…",
  tags: ["backend", "דחוף"],
  blocked_by: ["TKT-099"],
}
```

Comment (one file per comment, so concurrent comments never conflict):

```json5
{
  id: "20261018T100000123Z-ab12cd",   // = file name
  author: "Yoav <yoav@example.com>",     // from git config, set by the server
  created_at: "2026-10-18T10:00:00Z",
  edited_at: null,
  body: "…",
  attachments: [{ name: "screenshot.png", file: "attachments/20261018T100000123Z-ab12cd/screenshot.png", mime: "image/png", size: 48213 }],
}
```

Notes:
- Hand-written JSON5 (comments, unquoted keys, trailing commas) is read fine, but when the app saves a task it rewrites the file as deterministic JSON (valid JSON5), so `//` comments in that file are lost. Extra fields you add are kept.
- Attachments are user files committed to the repo; consider Git LFS for large ones. PNG/JPEG/GIF/WebP show inline; everything else (including SVG) is served as a download.
- Only the comment's author (by git identity) can edit or delete it.
- Uncommitted edits under `.todo/` are picked up too (the cache key is `HEAD` + the dirty-file list).

## Translations

```bash
uv run pybabel extract -F babel.cfg -k _ -k gettext -k ngettext:1,2 --no-location -o src/translations/messages.pot src
uv run pybabel update -i src/translations/messages.pot -d src/translations
uv run pybabel compile -d src/translations
```

## Development

```bash
uv run pytest                      # engine, sync (temp bare remote + 2 clones), routes, i18n
uv run ultra-todo --target . --debug   # Flask dev server with reload
```

Layout: `src/engine.py` (files, git, SQLite index, sync), `src/graph.py` (DAG), `src/i18n.py`, `src/app.py` (routes + CLI), `src/templates/`, `src/static/`, `src/translations/`.
