# GitFlow-Task

A self-hosted, local-first task manager written in Python (Flask). It manages **any local Git repository** that contains a `.todo/tasks/` directory of plain-text JSON5 files. Hebrew and English are both supported (UI, RTL layout, and content).

> Status: design phase. See [PLAN.md](PLAN.md) (English) and [plan_he.html](plan_he.html) (full plan in Hebrew).

## Core ideas

- **Zero-conflict GitOps** – the managed repo holds only text (`.json5`) written by the app. No database ever touches your project.
- **Smart persistent index** – a single SQLite file in the global cache dir (`~/.cache/gitflow-task/app_global_index.sqlite3`, or the OS equivalent) serves fast lookups for all projects.
- **Git hash invalidation** – on boot the app compares `git rev-parse HEAD` (plus uncommitted changes under `.todo/`) with the cached value. Unchanged → no disk scan; changed → only the modified files are re-parsed.
- **DAG dependencies** – "blocked by" links are checked with DFS cycle detection; cycles return HTTP 400.

## Features

- Kanban board and list view
- **Tags** on tasks, with filter chips (AND / OR, shareable via URL)
- **Comments** per task: newest first, author and time stamped by the server, editable, with image/file attachments stored next to the task
- **Update** button: `git pull --ff-only`, then the index is refreshed from the diff (rebase offered if the branch diverged; aborted cleanly on conflict)
- **Save work** button: commits only `.todo/` changes (editable message) and pushes; never force-pushes, never stages files outside `.todo/`
- Hebrew + English: translated UI, RTL layout, `dir="auto"` for mixed-language content; language set with `--lang` and/or the in-app toggle

## Install and run (uv)

```bash
uv sync
uv run gitflow-task --target /path/to/your/repo            # http://127.0.0.1:8080
uv run gitflow-task --target /path/to/repo --lang he --port 9000 --auto-commit
```

Global install: `uv tool install .` then `gitflow-task --target ...`.
Without uv: `pip install -r requirements.txt && python src/app.py --target ...`.

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
    ├── comments/2026-10-18T10-00-00Z-ab12cd.json5
    └── attachments/<comment-id>/screenshot.png
```

Task:

```json5
{
  id: "TKT-101",
  title: "תיקון באג בהתחברות",
  status: "in_progress",        // todo | in_progress | done | blocked
  assignee: "yoav",
  description: "…",
  tags: ["backend", "דחוף"],
  blocked_by: ["TKT-099"],
}
```

Comment (one file per comment, so concurrent comments never conflict):

```json5
{
  id: "ab12cd",
  author: "Yoav <yoav@example.com>",
  created_at: "2026-10-18T10:00:00Z",
  edited_at: null,
  body: "…",
  attachments: [{ name: "screenshot.png", file: "attachments/ab12cd/screenshot.png", mime: "image/png", size: 48213 }],
}
```

Attachments are user files committed to the repo; consider Git LFS for large ones.

## Translations

```bash
uv run pybabel extract -F babel.cfg -o src/translations/messages.pot src
uv run pybabel update -i src/translations/messages.pot -d src/translations
uv run pybabel compile -d src/translations
```

## Development

```bash
uv run pytest
```
