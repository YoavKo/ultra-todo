# ultra-todo — Implementation Plan (Flask, local-first, he + en)

## Context
Greenfield project in `ultra-todo/` (Python 3.12, git 2.43). Build the self-hosted GitOps task manager from the design doc: JSON5 task files in `<repo>/.todo/tasks/`, a global SQLite index in the OS cache dir, HEAD-hash cache invalidation, DAG dependency validation, served by Flask + waitress. Added requirement: **full Hebrew + English support** — translated UI, RTL layout, correct Hebrew content handling, language chosen via CLI default plus in-UI toggle; **tags** with filtering; per-task **comments** with attachments; **uv** support.

Added later: **priority** (high / medium / low); a **dependencies panel** on the task page (what blocks it and what it blocks, one-click removal); **several repos at once** with automatic free-port selection; a **known-repos list** with `ls` / `open` / `forget` commands; and a `utd` bash shortcut with tab completion.

## Layout
```
ultra-todo/
├── pyproject.toml            # uv-managed: deps, `[project.scripts] ultra-todo = "app:main"`, dev group (pytest)
├── uv.lock                   # committed lockfile
├── requirements.txt          # generated via `uv export` for non-uv users (flask, waitress, json5, flask-babel, platformdirs)
├── babel.cfg
├── src/
│   ├── app.py                # CLI (--target, --lang, --port, ls/open/forget…), routes, locale selector
│   ├── engine.py             # task file I/O, git helpers, SQLite index, hydration, sync
│   ├── repos.py              # known-repos list (repos.json)
│   ├── graph.py              # DAG + DFS cycle detection
│   ├── i18n.py               # Babel setup, locale resolution, direction helper
│   ├── translations/{he,en}/LC_MESSAGES/messages.po
│   ├── static/{app.css,app.js}
│   └── templates/{base,board,task,list}.html
└── tests/                    # pytest: engine, sync, routes, i18n, repos
```
(Design doc puts `templates/` beside `src/`; keeping it inside `src/` so Flask finds it by default — trivial to move.)

## Core engine (`engine.py`)
- **Cache path**: `platformdirs.user_cache_dir("ultra-todo")/app_global_index.sqlite3`. Tables `projects`, `tasks`, `dependencies` exactly as in the doc. Add `PRAGMA journal_mode=WAL`, `foreign_keys=ON`, and one connection per request/thread (waitress is multi-threaded); writes serialized with a lock.
- **Tags**: tasks get a `tags: []` list in the JSON5 file. Normalize on write (NFC, trim, collapse whitespace, case-fold for matching but preserve display casing of first use, dedupe, max length); Hebrew tags are fully supported. New SQLite table `task_tags(project_path, task_id, tag, tag_key)` with composite PK `(project_path, task_id, tag_key)` and an index on `(project_path, tag_key)`; `tag_key` is the casefolded/NFC form used for filtering. Hydration (full + differential) and mutations maintain it alongside `tasks`. Bump a `schema_version` (PRAGMA user_version) and rebuild the index on mismatch — the cache is disposable, so migration = drop + rehydrate.
- **Comments (discussion thread)**: each task owns a directory `.todo/tasks/<ID>/` (created lazily). One file per comment — `.todo/tasks/<ID>/comments/<UTC-ts>-<shortid>.json5` with `{id, author, created_at, edited_at?, body, attachments:[{name, file, mime, size}]}` — so two people commenting concurrently never touch the same file (zero-conflict pillar; a single `comments[]` array in the task file would conflict). Scanner globs only top-level `.todo/tasks/*.json5` for tasks and `<ID>/comments/*.json5` for comments.
  - **Author/time**: server stamps `created_at` (UTC ISO-8601) and author from `git config user.name`/`user.email` (falls back to OS user; `--author` override). Client-supplied values ignored. Displayed in the viewer's locale/timezone (Babel).
  - **Order**: newest first (`ORDER BY created_at DESC, id DESC`).
  - **Edit**: only the comment body (and adding/removing attachments) via `POST /task/<id>/comment/<cid>`; sets `edited_at`, shows an "edited" badge. Delete supported (removes file + its attachments). Edit/delete permitted only for the same author (local-trust tool, but prevents accidents) — easy to relax. Git history provides the real audit trail.
  - **Attachments** (images/files): stored at `.todo/tasks/<ID>/attachments/<comment-id>/<sanitized-name>`; filename sanitized (strip path parts, NFC, collision-suffix), size cap (default 10 MB, configurable `--max-attachment-mb`), served via `GET /task/<id>/attachment/<cid>/<name>` with `Content-Disposition` and `X-Content-Type-Options: nosniff`; only raster images (png/jpg/gif/webp) inline-rendered as thumbnails/lightbox — SVG/HTML always forced download to avoid script execution. Path traversal blocked by resolving and checking the path stays under the task dir.
  - **Caveat vs. design doc**: attachments are user content in the repo (not DB binaries), so the "text-only repo" pillar becomes "no *app* binaries"; document recommending Git LFS for large files. Auto-commit stages the comment file + its attachments only.
  - **Index**: table `comments(project_path, task_id, id, author, created_at, edited_at, body)`, composite PK `(project_path, task_id, id)`, index on `(project_path, task_id, created_at DESC)`; `attachments` rows optional (or derived from the comment file). Hydration/differential hydration/delete handling extended to comment paths; comment body also searchable. Task card shows a comment count.
  - **Rendering**: comment body is escaped plain text with auto-linking and line breaks (or sanitized Markdown, decision deferred); `dir="auto"` for Hebrew/English mixes. Paste-an-image and drag-drop upload in the comment box (vanilla JS, `multipart/form-data`, CSRF token).
- **Task file**: `.todo/tasks/<ID>.json5` — fields `id,title,status,priority,assignee,description,tags[],blocked_by[]` (+ optional `lang`). Extra user fields are kept on save. Read with `json5`; write deterministically (sorted keys, 2-space indent, trailing newline, `ensure_ascii=False` so Hebrew stays readable and diffs are clean). Write atomically (temp file + `os.replace`). Validate `ID` against `^[A-Za-z0-9_-]+$` to prevent path traversal.
- **Boot/hydration**: `git rev-parse HEAD` → compare with `projects.last_scanned_commit`.
  - Hit → skip scan. Missing row → full hydrate. Differ → `git diff --name-status <old> HEAD -- .todo/tasks` and handle A/M/D/R (deletions remove rows; renames = delete+add). Fall back to full hydrate if `<old>` is unreachable (rebase/gc) or diff fails.
  - **Gap in the doc to resolve**: HEAD alone misses *uncommitted* edits to `.json5` files (hand edits, `--auto-commit` off). Plan: also fold `git status --porcelain -- .todo/tasks` into the cache key (HEAD + hash of dirty-file list/mtimes); if dirty, re-parse just those files. Non-git targets: error out with a clear message.
- **Mutation**: validate → graph check → write file → update SQLite in one transaction → if `--auto-commit`, `git add <file>` + `git commit -m "task: update <ID>"` (only that path) → store new HEAD in `last_scanned_commit`. If the file write fails, no DB change; if the commit fails, report but keep file+DB consistent (cache key uses dirty state, so it self-heals).

## Sync buttons: Update and Save work (`engine.py` git helpers, header buttons)
- **Update** (`POST /sync/pull`): `git pull --ff-only` in the target repo, then run the existing differential hydration from the old HEAD to the new HEAD (A/M/D/R for tasks, comments and tags), so only changed files are re-parsed. Shows a summary toast ("3 tasks updated, 1 added, 2 comments"). Rules:
  - Needs an upstream; with no remote/upstream the button is disabled with a tooltip.
  - If the branch diverged (ff-only fails), offer "Update with rebase": `git pull --rebase --autostash`. On a conflict run `git rebase --abort` (and restore the stash), then report the conflicting files so the user resolves them in git. The app never leaves the repo mid-rebase.
  - Uncommitted `.todo/` edits are handled by `--autostash`; the cache key (HEAD + dirty list) is recomputed afterwards.
- **Save work** (`POST /sync/save`): stage only `.todo/` (`git add -- .todo`), commit with an editable message (default `tasks: update N task(s)`), then `git push`. Rules:
  - Never stages anything outside `.todo/`. If nothing is staged, skip the commit and just push any unpushed commits.
  - Push rejected (remote ahead) → no force push; tell the user to run Update first. No upstream → offer `git push -u origin HEAD`.
  - After the commit, store the new HEAD in `last_scanned_commit`. With `--auto-commit` on, Save is effectively just the push.
- **Shared plumbing**: all git calls go through one helper with `GIT_TERMINAL_PROMPT=0` and a timeout (no hangs on credential prompts), run under a per-project lock so they cannot interleave with mutations; stderr is surfaced to the user in a translated error dialog. POST + CSRF only.
- **Status** (`GET /api/sync-status`): branch, upstream, ahead/behind (from local refs, no network), count of uncommitted `.todo/` changes. Drives a badge on Save work ("5 changes") and "↑2 ↓1" next to the branch name; Update also runs `git fetch` so behind-counts refresh when clicked.
- Buttons are labeled via Babel (Update / עדכן, Save work / שמור עבודה), show a spinner while running, and are disabled during an operation.

## Graph (`graph.py`)
Build adjacency from `dependencies` for the project, add the proposed edge(s), iterative DFS with white/gray/black coloring (avoids recursion limit); return the cycle path. Routes return HTTP 400 with a translated message plus the cycle path. Also reject self-dependency and unknown IDs.

### Dependencies on the task page
- A "Dependencies" panel near the top of the task page with two lists: **Blocked by** and **Blocks**. Each row shows status, ID and title, linked to the other task.
- **×** on a row removes that link at once (`POST /task/<id>/blocked_by/<blocker>/delete`), from either side; the user stays on the page they were on.
- A picker + Add in each list (`POST /task/<id>/dependency` with `kind=blocked_by|blocks`); a link that would create a cycle is rejected with a message.
- A "Waiting on N open tasks" warning when blockers aren't done. Board cards show ⛔ N (blocked by) and ➜ N (blocks).
- Saving the main task form never changes blockers (so they can't be cleared by accident); only the New task form picks blockers, with checkboxes.

## Priority
- `priority` field: `high | medium | low`, default `medium` (also for existing tasks without it). Other values fail validation.
- Index: `priority` and `priority_rank` columns on `tasks` (bumped `SCHEMA_VERSION` → index rebuilt automatically).
- UI: coloured badge (▲ high, ● medium, ▼ low) on cards and in the list, plus a coloured card edge; form field, list column and a filter in the filter bar.
- Sorting: board columns and the list order by priority (high first), then ID.

## Hebrew + English (`i18n.py`, templates, CSS)
- **UI strings**: Flask-Babel, all templates use `_()`; `.po` catalogs for `en` and `he`; `pybabel extract/init/compile` documented. Plural forms handled per catalog (Hebrew has distinct plural rules).
- **Locale resolution order**: `?lang=` → cookie `lang` (set by header toggle) → CLI `--lang` default → `Accept-Language` best match → `en`.
- **Direction**: `<html lang="{{locale}}" dir="{{'rtl' if locale=='he' else 'ltr'}}">`. CSS written with **logical properties** (`margin-inline-start`, `padding-inline`, `text-align: start`, `inset-inline-*`) so one stylesheet serves both; Kanban column order, drag-drop affordances and icons that imply direction (arrows, chevrons) flip via `[dir=rtl]`.
- **Content**: task title/description rendered in elements with `dir="auto"` (or the task's `lang` if set) so a Hebrew task inside an English UI (and vice versa) displays correctly; inputs also `dir="auto"`; IDs/hashes/paths wrapped in `<bdi>`/`dir="ltr"`. Fonts with Hebrew glyphs in the stack.
- **Data handling**: UTF-8 everywhere (file I/O, SQLite, HTTP). Status values stay as stable English keys in the files (`todo|in_progress|done|blocked`) and are *translated only at display time*, so repos stay language-neutral. Search uses Unicode-aware case-folding and NFC normalization (Hebrew niqqud/geresh handled by normalizing on both index and query).
- **Dates/numbers** formatted via Babel for the active locale.

## UI / routes (`app.py`)
`/` Kanban board by status (filters: assignee, text, priority, **tags**), `/list`, `/task/<id>` view/edit, `POST /task` create, `POST /task/<id>` update, `POST /task/<id>/status` (drag-drop), `POST /lang` (set cookie), `GET /api/graph`, `GET /api/tags` (all tags + counts, for autocomplete/filter chips), `POST /task/<id>/comment` (create, multipart), `POST /task/<id>/comment/<cid>` (edit), `POST /task/<id>/comment/<cid>/delete`, `GET /task/<id>/attachment/<cid>/<name>`, `POST /task/<id>/dependency` and `POST /task/<id>/blocked_by/<blocker>/delete` (add/remove links), `POST /sync/pull`, `POST /sync/save`, `GET /api/sync-status`. The task page shows the comment composer above a newest-first thread.
- **Tag UI/filtering**: tag chips on cards; tag input with autocomplete (Enter/comma to add, Backspace removes) in the task form; filter bar of clickable tag chips on board and list. Filter state lives in the URL (`?tag=a&tag=b&match=any|all`, default `all`/AND; "any" toggle) so views are shareable/bookmarkable; query uses `tag_key` joins with `GROUP BY ... HAVING COUNT(DISTINCT tag_key)=N` for AND. Combines with assignee/text filters. Chips use `dir="auto"` so Hebrew tags render correctly; tag-name UI labels translated. Small vanilla JS for drag-and-drop and language toggle. Launch: `uv run ultra-todo --target PATH [--lang he|en] [--port N] [--auto-commit]` (or `python src/app.py ...`), served via `waitress.serve` bound to 127.0.0.1 by default (local-only tool; also add CSRF token on POSTs).

## Several repos and the command line
- **Several instances at once**: without `--port`, each instance takes the first free port from 8080 up (8080, 8081, …) and logs which repo is served where. The socket is bound up front and handed to waitress, so there is no check-then-bind race. A busy explicit `--port` → one-line error instead of a traceback. The repo name is in the browser tab title. Instances safely share the cache file (WAL, per-project rows).
- **Known-repos list** (`repos.py`): every served repo is recorded in `repos.json` in the user data dir (`~/.local/share/ultra-todo/` on Linux) — not the cache, so clearing the cache doesn't lose it. Repos already in the cache index are imported. (A custom `--db` is not recorded.)
- **`ultra-todo ls`**: numbered list, most recently opened first. Columns: number, last opened (local time), `[x open, y done]`, name, path; `(missing)` if the directory is gone. Open = todo, in progress and blocked. Each repo's index is refreshed first (a cheap cache-hit check), so counts include `git pull`s and uncommitted edits.
- **`ultra-todo open NAME|NUMBER [flags]`**: serve a known repo by name (case-insensitive) or `ls` number; names win over numbers. Two repos with the same name → error listing both paths. Numbers follow the `ls` order, so they shift when another repo is opened (`open 1` = the last one).
- **`ultra-todo forget NAME|NUMBER,...`**: remove repos from the list and their rows from the cache index (otherwise they'd be re-imported). Names and numbers, space- or comma-separated (`3,5`). All are resolved against one listing before anything is removed; if one is unknown, nothing is removed. Missing repos can be forgotten too. The repo's files are never touched; serving it again adds it back.
- **Help**: `-h`/`--help` shows the flags and a commands list; each command has its own help with examples (`ls -h`, `open -h`, `forget -h`).
- **`utd` shortcut** (bash function in `~/.bashrc`, full code in the README): `utd [PATH] [flags]` runs `uv run --project <checkout> ultra-todo --target <absolute path>` from any directory; the path defaults to the current directory, and flags alone (`utd --lang he`) still use it. `utd ls|open|forget` pass through. `utd` sets `ULTRA_TODO_PROG=utd` so help and errors speak in `utd` terms (and `--target` is hidden). Tab completion: commands and directories first, repo names after `open`/`forget` (also after a comma), then flags (`--lang` completes `en`/`he`). For a directory named `ls`, write `utd ./ls`.

## Build order
**First action after approval** (plan mode only allows editing this file): create docs only (no app code yet): `ultra-todo/PLAN.md` (this plan), `ultra-todo/plan_he.html` (full Hebrew translation of the whole plan, standalone RTL page with `lang="he" dir="rtl"`, Hebrew-capable font, code/paths kept LTR via `<bdi>`/`dir="ltr"`, works in light/dark, printable), and `ultra-todo/README.md` (overview, pillars, repo/cache layout, task + comment file formats, `uv` install/run, CLI flags, he/en usage, `pybabel` workflow, Git LFS note for attachments, dev/test commands). Keep both updated as the design changes.

0. `uv init` / `uv add flask waitress json5 flask-babel platformdirs`, `uv add --dev pytest`; run with `uv run ultra-todo --target PATH` or `uv run python src/app.py ...`; tests via `uv run pytest`. Python pinned via `requires-python = ">=3.11"` (`.python-version` = 3.12). Optional: `uvx --from . ultra-todo` / `uv tool install .` for a global install, matching the "global engine" idea.
1. Skeleton, `engine.py` (incl. tags) (file I/O + SQLite + full hydration) with tests.
2. Git hash/dirty-state invalidation + differential hydration with tests (temp git repos).
3. `graph.py` + tests (cycles, self-loop, diamond that is *not* a cycle).
4. Flask routes + templates (en only), mutation flow, optional auto-commit, Update / Save work buttons and `/api/sync-status`.
5. i18n: Babel wiring, extract strings, write `he` catalog, RTL CSS, `dir=auto` content, toggle + CLI flag.
6. Polish: error pages, README with usage and `pybabel` commands.
7. Later additions: dependencies panel, priority, automatic free port, known-repos list (`ls`/`open`/`forget`), `utd` shortcut with tab completion.

## Verification
- Tags: `uv run pytest` cases — tag normalization/dedupe, Hebrew tag round-trip, AND vs OR filtering, tag edit updates `task_tags`, differential hydration removes tags of deleted tasks, `/api/tags` counts.
- Comments: newest-first ordering; author/timestamp server-stamped; edit sets `edited_at` and rejects other authors; attachment upload/download round-trip; filename sanitization + path traversal rejected; SVG served as download; oversize rejected; two comments created concurrently produce distinct files; deleting a task's comment via git (differential hydration) removes the row; Hebrew comment renders with correct direction.
- Sync (tests use a temp bare remote + two clones): Update fast-forwards and the index reflects added/changed/deleted tasks and comments; diverged branch → rebase offer; rebase conflict → aborted, repo clean, files listed; Save stages only `.todo/` (a file outside it stays unstaged); push rejected → friendly error, no force; no upstream → Update disabled, Save offers `push -u`; status badge counts and ahead/behind are correct; no operation hangs on a credential prompt.
- Dependencies and priority: remove/add a link from either side; a cycle-making link is rejected; saving the task form keeps blockers; default `medium`, invalid value rejected, high → medium → low ordering, priority filter, Hebrew labels.
- Repos and ports: two instances without `--port` get different ports; a busy explicit port → clear error; record and open by name (case-insensitive) and by number; duplicate name → error; missing repo skipped by `open`; import from the cache; `forget` by numbers and names, all-or-nothing, not re-imported; `[open, done]` counts include uncommitted edits.
- `uv sync` from clean checkout works; `uv run ultra-todo --help` runs.
- `pytest`: hydration hit/miss/diff/delete/rename; uncommitted edit detected; cycle → 400; path-traversal ID rejected; Hebrew title round-trips through file → SQLite → HTML unchanged; locale resolution order; `dir` attribute per locale.
- Manual: create a scratch git repo with `.todo/tasks/`, run the app, confirm second boot skips scanning (log line "cache hit"), edit a file by hand and by `git pull`-style commit and confirm refresh. Switch he↔en: layout mirrors, Hebrew task with an English ID displays correctly, drag-drop works in RTL. Confirm `git status` of the target repo shows only `.json5` changes (no binaries).

## Open points (defaults chosen, easy to change)
Uncommitted-change handling as above; bind to localhost only; statuses fixed to four values (configurable later via `.todo/config.json5`).
