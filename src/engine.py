"""File I/O, Git evaluation and the global SQLite index.

The managed repository holds only text files under ``.todo/tasks``; the index
lives in the OS cache directory and is a disposable, rebuildable cache.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import unicodedata
from pathlib import Path
from typing import Any, Iterable

import json5
import platformdirs

log = logging.getLogger("ultra-todo")

SCHEMA_VERSION = 2
STATUSES = ("todo", "in_progress", "done", "blocked")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_TAG_LEN = 40
TODO_DIR = ".todo"
TASKS_REL = ".todo/tasks"


class EngineError(Exception):
    """Base class for user-facing engine errors."""


class ValidationError(EngineError):
    pass


class NotFound(EngineError):
    pass


class CycleError(ValidationError):
    def __init__(self, cycle: list[str]):
        super().__init__(" -> ".join(cycle))
        self.cycle = cycle


class GitError(EngineError):
    pass


class SyncError(EngineError):
    """A sync operation that needs user action. ``code`` is stable for i18n."""

    def __init__(self, code: str, detail: str = "", files: list[str] | None = None):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail
        self.files = files or []


# --------------------------------------------------------------------------
# text helpers
# --------------------------------------------------------------------------

_HEB_MARKS = {chr(c) for c in range(0x0591, 0x05C8)
              if unicodedata.category(chr(c)) == "Mn"}
_PUNCT_MAP = {"׳": "'", "״": '"'}


def fold(text: str) -> str:
    """Unicode-aware key for matching: NFC, casefold, no niqqud, geresh->'."""
    text = unicodedata.normalize("NFC", text or "")
    text = "".join(_PUNCT_MAP.get(c, c) for c in text if c not in _HEB_MARKS)
    return text.casefold()


def normalize_tag(tag: str) -> str:
    tag = unicodedata.normalize("NFC", str(tag))
    tag = re.sub(r"\s+", " ", tag).strip()
    return tag[:MAX_TAG_LEN]


def normalize_tags(tags: Iterable[str] | None) -> list[str]:
    """Normalise, drop empties, dedupe by folded key (first spelling wins)."""
    seen: dict[str, str] = {}
    for t in tags or []:
        t = normalize_tag(t)
        if t and fold(t) not in seen:
            seen[fold(t)] = t
    return list(seen.values())


def valid_id(task_id: str) -> str:
    if not isinstance(task_id, str) or not ID_RE.match(task_id):
        raise ValidationError(f"invalid task id: {task_id!r}")
    return task_id


def sanitize_filename(name: str) -> str:
    name = unicodedata.normalize("NFC", name or "").replace("\\", "/")
    name = name.rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", name).strip(" .")
    if not name or name in (".", ".."):
        name = "file"
    if len(name.encode()) > 150:
        stem, dot, ext = name.rpartition(".")
        name = (stem or name)[:100] + (dot + ext[:10] if dot else "")
    return name


# --------------------------------------------------------------------------
# git helpers
# --------------------------------------------------------------------------

def run_git(repo: Path | str, *args: str, check: bool = True,
            timeout: float = 60, input: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C.UTF-8")
    env.setdefault("GIT_ASKPASS", "echo")
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env, timeout=timeout, input=input)
    except FileNotFoundError as e:
        raise GitError("git executable not found") from e
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git {' '.join(args[:2])} timed out") from e
    if check and proc.returncode != 0:
        raise GitError((proc.stderr or proc.stdout).strip() or f"git {args[0]} failed")
    return proc


def git_root(path: Path) -> Path | None:
    p = run_git(path, "rev-parse", "--show-toplevel", check=False)
    return Path(p.stdout.strip()).resolve() if p.returncode == 0 else None


# --------------------------------------------------------------------------
# task / comment files
# --------------------------------------------------------------------------

def dump_file(path: Path, data: dict[str, Any]) -> None:
    """Deterministic, human-readable (valid JSON5) atomic write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_file(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data = json5.load(f)
    if not isinstance(data, dict):
        raise ValidationError(f"{path.name}: expected an object")
    return data


def clean_task(raw: dict[str, Any], task_id: str | None = None) -> dict[str, Any]:
    """Coerce a raw dict into the canonical task shape."""
    tid = valid_id(task_id or raw.get("id"))
    status = raw.get("status") or "todo"
    if status not in STATUSES:
        raise ValidationError(f"invalid status: {status!r}")
    bb = raw.get("blocked_by") or []
    if isinstance(bb, str):
        bb = [bb]
    blocked_by = sorted({valid_id(x) for x in bb})
    if tid in blocked_by:
        raise ValidationError("a task cannot block itself")
    task = {
        "id": tid,
        "title": unicodedata.normalize("NFC", str(raw.get("title") or "").strip()),
        "status": status,
        "assignee": str(raw.get("assignee") or "").strip(),
        "description": str(raw.get("description") or ""),
        "tags": normalize_tags(raw.get("tags")),
        "blocked_by": blocked_by,
    }
    lang = raw.get("lang")
    if lang:
        task["lang"] = str(lang)
    return task


# --------------------------------------------------------------------------
# paths <-> entities
# --------------------------------------------------------------------------

_TASK_PATH = re.compile(r"^\.todo/tasks/([A-Za-z0-9_-]+)\.json5$")
_COMMENT_PATH = re.compile(r"^\.todo/tasks/([A-Za-z0-9_-]+)/comments/([^/]+)\.json5$")


def classify_path(rel: str) -> tuple[str, str, str | None] | None:
    m = _TASK_PATH.match(rel)
    if m:
        return ("task", m.group(1), None)
    m = _COMMENT_PATH.match(rel)
    if m:
        return ("comment", m.group(1), m.group(2))
    return None


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
  project_path TEXT PRIMARY KEY,
  last_scanned_commit TEXT,
  dirty_key TEXT DEFAULT '',
  dirty_files TEXT DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS tasks(
  project_path TEXT, id TEXT, title TEXT, status TEXT, assignee TEXT,
  description TEXT, lang TEXT, search TEXT,
  PRIMARY KEY(project_path, id)
);
CREATE TABLE IF NOT EXISTS dependencies(
  project_path TEXT, task_id TEXT, blocked_by_id TEXT,
  PRIMARY KEY(project_path, task_id, blocked_by_id)
);
CREATE TABLE IF NOT EXISTS task_tags(
  project_path TEXT, task_id TEXT, tag TEXT, tag_key TEXT,
  PRIMARY KEY(project_path, task_id, tag_key)
);
CREATE INDEX IF NOT EXISTS idx_tags_key ON task_tags(project_path, tag_key);
CREATE TABLE IF NOT EXISTS comments(
  project_path TEXT, task_id TEXT, id TEXT, author TEXT, created_at TEXT,
  edited_at TEXT, body TEXT, attachments TEXT DEFAULT '[]', search TEXT,
  PRIMARY KEY(project_path, task_id, id)
);
CREATE INDEX IF NOT EXISTS idx_comments_task
  ON comments(project_path, task_id, created_at DESC);
"""


def default_db_path() -> Path:
    return Path(platformdirs.user_cache_dir("ultra-todo")) / "app_global_index.sqlite3"


class Summary(dict):
    """Counts of what a refresh changed."""

    def __init__(self):
        super().__init__(tasks_added=0, tasks_updated=0, tasks_deleted=0,
                         comments_changed=0, full=False)

    @property
    def changed(self) -> bool:
        return any(v for k, v in self.items() if k != "full") or self["full"]


class Engine:
    def __init__(self, target: str | Path, db_path: str | Path | None = None,
                 auto_commit: bool = False, author: str | None = None,
                 max_attachment_bytes: int = 10 * 1024 * 1024):
        target = Path(target).expanduser().resolve()
        if not target.is_dir():
            raise EngineError(f"target is not a directory: {target}")
        root = git_root(target)
        if root is None:
            raise EngineError(f"target is not a git repository: {target}")
        self.root = root
        self.project = str(root)
        self.tasks_dir = root / TASKS_REL
        self.auto_commit = auto_commit
        self.author_override = author
        self.max_attachment_bytes = max_attachment_bytes
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._local = threading.local()
        self.warnings: list[str] = []
        self._init_db()

    # ---- db ----------------------------------------------------------
    @property
    def db(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        with self.lock:
            ver = self.db.execute("PRAGMA user_version").fetchone()[0]
            if ver != SCHEMA_VERSION:
                # the index is disposable: drop and rehydrate
                for t in ("projects", "tasks", "dependencies", "task_tags", "comments"):
                    self.db.execute(f"DROP TABLE IF EXISTS {t}")
                self.db.executescript(SCHEMA)
                self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            else:
                self.db.executescript(SCHEMA)

    class _Tx:
        def __init__(self, conn): self.conn = conn
        def __enter__(self): self.conn.execute("BEGIN IMMEDIATE"); return self.conn
        def __exit__(self, et, ev, tb):
            self.conn.execute("ROLLBACK" if et else "COMMIT")

    def tx(self) -> "_Tx":
        return Engine._Tx(self.db)

    # ---- git state ---------------------------------------------------
    def head(self) -> str:
        p = run_git(self.root, "rev-parse", "HEAD", check=False)
        return p.stdout.strip() if p.returncode == 0 else ""

    def dirty_files(self) -> list[str]:
        """Repo-relative paths under .todo with uncommitted changes."""
        p = run_git(self.root, "status", "--porcelain", "-z", "-uall", "--", TODO_DIR)
        out: list[str] = []
        parts = p.stdout.split("\0")
        i = 0
        while i < len(parts):
            entry = parts[i]
            i += 1
            if len(entry) < 4:
                continue
            xy, path = entry[:2], entry[3:]
            out.append(path)
            if "R" in xy or "C" in xy:  # next field is the original path
                if i < len(parts) and parts[i]:
                    out.append(parts[i])
                i += 1
        return sorted(set(out))

    def _fingerprint(self, files: list[str]) -> str:
        h = hashlib.sha1()
        for rel in files:
            try:
                st = (self.root / rel).stat()
                h.update(f"{rel}\0{st.st_mtime_ns}\0{st.st_size}\n".encode())
            except FileNotFoundError:
                h.update(f"{rel}\0gone\n".encode())
        return h.hexdigest() if files else ""

    # ---- hydration ---------------------------------------------------
    def refresh(self) -> Summary:
        """Bring the index in sync with the repo (cheap when nothing changed)."""
        with self.lock:
            summary = Summary()
            head = self.head()
            dirty = self.dirty_files()
            fp = self._fingerprint(dirty)
            row = self.db.execute(
                "SELECT * FROM projects WHERE project_path=?", (self.project,)).fetchone()

            if row is None:
                self._full_hydrate(summary)
            elif row["last_scanned_commit"] != head:
                paths = self._paths_between(row["last_scanned_commit"], head)
                if paths is None:
                    self._full_hydrate(summary)
                else:
                    paths = set(paths) | set(json.loads(row["dirty_files"])) | set(dirty)
                    self._refresh_paths(sorted(paths), summary)
            elif row["dirty_key"] != fp:
                paths = set(json.loads(row["dirty_files"])) | set(dirty)
                self._refresh_paths(sorted(paths), summary)
            else:
                log.debug("cache hit for %s @ %s", self.project, head[:8])
                return summary

            self._store_state(head, dirty, fp)
            return summary

    def _store_state(self, head: str, dirty: list[str], fp: str) -> None:
        self.db.execute(
            "INSERT INTO projects(project_path,last_scanned_commit,dirty_key,dirty_files)"
            " VALUES(?,?,?,?) ON CONFLICT(project_path) DO UPDATE SET"
            " last_scanned_commit=excluded.last_scanned_commit,"
            " dirty_key=excluded.dirty_key, dirty_files=excluded.dirty_files",
            (self.project, head, fp, json.dumps(dirty)))

    def record_state(self) -> None:
        """Mark the repo's current state as indexed (after our own writes)."""
        with self.lock:
            dirty = self.dirty_files()
            self._store_state(self.head(), dirty, self._fingerprint(dirty))

    def _paths_between(self, old: str, new: str) -> list[str] | None:
        """Changed .todo/tasks paths between commits, or None => full rescan."""
        if not old or not new:
            return None
        if run_git(self.root, "cat-file", "-e", f"{old}^{{commit}}", check=False).returncode:
            return None
        p = run_git(self.root, "diff", "--name-status", "-z", "--no-renames",
                    old, new, "--", TASKS_REL, check=False)
        if p.returncode != 0:
            return None
        parts = [x for x in p.stdout.split("\0") if x]
        return parts[1::2]  # [status, path, status, path, ...]

    def _full_hydrate(self, summary: Summary) -> None:
        summary["full"] = True
        paths: list[str] = []
        if self.tasks_dir.is_dir():
            for f in self.tasks_dir.glob("*.json5"):
                paths.append(f.relative_to(self.root).as_posix())
            for f in self.tasks_dir.glob("*/comments/*.json5"):
                paths.append(f.relative_to(self.root).as_posix())
        with self.tx() as db:
            for t in ("tasks", "dependencies", "task_tags", "comments"):
                db.execute(f"DELETE FROM {t} WHERE project_path=?", (self.project,))
        self._refresh_paths(sorted(paths), Summary())
        summary["tasks_added"] = self.db.execute(
            "SELECT COUNT(*) FROM tasks WHERE project_path=?", (self.project,)).fetchone()[0]

    def _refresh_paths(self, rels: list[str], summary: Summary) -> None:
        # tasks first so comment rows always have a parent task
        items = [(classify_path(r), r) for r in rels]
        items = [(c, r) for c, r in items if c]
        items.sort(key=lambda x: x[0][0] != "task")
        with self.tx() as db:
            for (kind, tid, cid), rel in items:
                path = self.root / rel
                try:
                    if kind == "task":
                        self._sync_task(db, tid, path, summary)
                    else:
                        self._sync_comment(db, tid, cid, path, summary)
                except (ValidationError, ValueError, OSError) as e:
                    msg = f"{rel}: {e}"
                    log.warning("skipping %s", msg)
                    self.warnings.append(msg)

    def _sync_task(self, db, tid: str, path: Path, summary: Summary) -> None:
        existed = db.execute("SELECT 1 FROM tasks WHERE project_path=? AND id=?",
                             (self.project, tid)).fetchone()
        if not path.is_file():
            if existed:
                self._delete_task_rows(db, tid)
                summary["tasks_deleted"] += 1
            return
        task = clean_task(load_file(path), tid)
        self._upsert_task(db, task)
        summary["tasks_updated" if existed else "tasks_added"] += 1

    def _sync_comment(self, db, tid: str, cid: str, path: Path, summary: Summary) -> None:
        db.execute("DELETE FROM comments WHERE project_path=? AND task_id=? AND id=?",
                   (self.project, tid, cid))
        if path.is_file():
            c = load_file(path)
            self._insert_comment(db, tid, cid, c)
        summary["comments_changed"] += 1

    # ---- row writers -------------------------------------------------
    def _upsert_task(self, db, task: dict[str, Any]) -> None:
        p, tid = self.project, task["id"]
        search = fold(" ".join([tid, task["title"], task["description"], task["assignee"]]))
        db.execute(
            "INSERT OR REPLACE INTO tasks(project_path,id,title,status,assignee,"
            "description,lang,search) VALUES(?,?,?,?,?,?,?,?)",
            (p, tid, task["title"], task["status"], task["assignee"],
             task["description"], task.get("lang"), search))
        db.execute("DELETE FROM dependencies WHERE project_path=? AND task_id=?", (p, tid))
        db.executemany("INSERT OR IGNORE INTO dependencies VALUES(?,?,?)",
                       [(p, tid, b) for b in task["blocked_by"]])
        db.execute("DELETE FROM task_tags WHERE project_path=? AND task_id=?", (p, tid))
        db.executemany("INSERT OR IGNORE INTO task_tags VALUES(?,?,?,?)",
                       [(p, tid, t, fold(t)) for t in task["tags"]])

    def _delete_task_rows(self, db, tid: str) -> None:
        for t, col in (("tasks", "id"), ("dependencies", "task_id"),
                       ("task_tags", "task_id"), ("comments", "task_id")):
            db.execute(f"DELETE FROM {t} WHERE project_path=? AND {col}=?", (self.project, tid))
        # other tasks may still list this id as a blocker; keep those edges
        # (the file is the source of truth) but they no longer resolve.

    def _insert_comment(self, db, tid: str, cid: str, c: dict[str, Any]) -> None:
        body = str(c.get("body") or "")
        db.execute(
            "INSERT OR REPLACE INTO comments(project_path,task_id,id,author,created_at,"
            "edited_at,body,attachments,search) VALUES(?,?,?,?,?,?,?,?,?)",
            (self.project, tid, cid, str(c.get("author") or ""),
             str(c.get("created_at") or ""), c.get("edited_at"), body,
             json.dumps(c.get("attachments") or [], ensure_ascii=False), fold(body)))

    # ---- queries -----------------------------------------------------
    def _task_rows(self, rows) -> list[dict[str, Any]]:
        tasks = [dict(r) for r in rows]
        if not tasks:
            return tasks
        p = self.project
        tags: dict[str, list[str]] = {}
        for r in self.db.execute(
                "SELECT task_id, tag FROM task_tags WHERE project_path=? ORDER BY tag_key", (p,)):
            tags.setdefault(r["task_id"], []).append(r["tag"])
        deps: dict[str, list[str]] = {}
        for r in self.db.execute(
                "SELECT task_id, blocked_by_id FROM dependencies WHERE project_path=? "
                "ORDER BY blocked_by_id", (p,)):
            deps.setdefault(r["task_id"], []).append(r["blocked_by_id"])
        counts = {r["task_id"]: r["n"] for r in self.db.execute(
            "SELECT task_id, COUNT(*) n FROM comments WHERE project_path=? GROUP BY task_id", (p,))}
        for t in tasks:
            t.pop("search", None)
            t["tags"] = tags.get(t["id"], [])
            t["blocked_by"] = deps.get(t["id"], [])
            t["comment_count"] = counts.get(t["id"], 0)
        return tasks

    def list_tasks(self, status: str | None = None, assignee: str | None = None,
                   q: str | None = None, tags: Iterable[str] = (),
                   match: str = "all") -> list[dict[str, Any]]:
        sql = ["SELECT * FROM tasks WHERE project_path=?"]
        args: list[Any] = [self.project]
        if status:
            sql.append("AND status=?"); args.append(status)
        if assignee:
            sql.append("AND assignee=?"); args.append(assignee)
        if q:
            like = "%" + re.sub(r"([\\%_])", r"\\\1", fold(q.strip())) + "%"
            sql.append("AND (search LIKE ? ESCAPE '\\' OR id IN ("
                       "SELECT task_id FROM comments WHERE project_path=? AND search LIKE ? ESCAPE '\\'))")
            args += [like, self.project, like]
        keys = sorted({fold(normalize_tag(t)) for t in tags if normalize_tag(t)})
        if keys:
            marks = ",".join("?" * len(keys))
            having = f"HAVING COUNT(DISTINCT tag_key)={len(keys)}" if match != "any" else ""
            sql.append(f"AND id IN (SELECT task_id FROM task_tags WHERE project_path=? "
                       f"AND tag_key IN ({marks}) GROUP BY task_id {having})")
            args += [self.project, *keys]
        sql.append("ORDER BY id")
        return self._task_rows(self.db.execute(" ".join(sql), args).fetchall())

    def get_task(self, task_id: str) -> dict[str, Any]:
        rows = self.db.execute("SELECT * FROM tasks WHERE project_path=? AND id=?",
                               (self.project, task_id)).fetchall()
        if not rows:
            raise NotFound(task_id)
        task = self._task_rows(rows)[0]
        task["blocks"] = [r["task_id"] for r in self.db.execute(
            "SELECT task_id FROM dependencies WHERE project_path=? AND blocked_by_id=? "
            "ORDER BY task_id", (self.project, task_id))]
        return task

    def all_tags(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT tag_key, MIN(tag) tag, COUNT(*) n FROM task_tags WHERE project_path=? "
            "GROUP BY tag_key ORDER BY n DESC, tag_key", (self.project,))
        return [{"tag": r["tag"], "key": r["tag_key"], "count": r["n"]} for r in rows]

    def assignees(self) -> list[str]:
        return [r[0] for r in self.db.execute(
            "SELECT DISTINCT assignee FROM tasks WHERE project_path=? AND assignee!='' "
            "ORDER BY assignee", (self.project,))]

    def dependency_graph(self) -> dict[str, list[str]]:
        adj: dict[str, list[str]] = {}
        for r in self.db.execute(
                "SELECT task_id, blocked_by_id FROM dependencies WHERE project_path=?",
                (self.project,)):
            adj.setdefault(r["task_id"], []).append(r["blocked_by_id"])
        return adj

    def get_comments(self, task_id: str) -> list[dict[str, Any]]:
        out = []
        for r in self.db.execute(
                "SELECT * FROM comments WHERE project_path=? AND task_id=? "
                "ORDER BY created_at DESC, id DESC", (self.project, task_id)):
            d = dict(r)
            d.pop("search", None)
            d["attachments"] = json.loads(d["attachments"] or "[]")
            out.append(d)
        return out

    # ---- mutations ---------------------------------------------------
    def task_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{valid_id(task_id)}.json5"

    def task_dir(self, task_id: str) -> Path:
        return self.tasks_dir / valid_id(task_id)

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def next_id(self, prefix: str = "TKT-") -> str:
        nums = [int(m.group(1)) for r in self.db.execute(
            "SELECT id FROM tasks WHERE project_path=?", (self.project,))
            if (m := re.match(re.escape(prefix) + r"(\d+)$", r[0]))]
        n = max(nums, default=100) + 1
        while self.task_path(f"{prefix}{n}").exists():
            n += 1
        return f"{prefix}{n}"

    def _check_graph(self, task: dict[str, Any]) -> None:
        from graph import cycle_after_setting

        known = {r[0] for r in self.db.execute(
            "SELECT id FROM tasks WHERE project_path=?", (self.project,))}
        unknown = [b for b in task["blocked_by"] if b not in known]
        if unknown:
            raise ValidationError(f"unknown task id(s): {', '.join(unknown)}")
        cycle = cycle_after_setting(self.dependency_graph(), task["id"], task["blocked_by"])
        if cycle:
            raise CycleError(cycle)

    def _commit(self, paths: list[Path], message: str) -> str | None:
        """Auto-commit just these paths. Returns an error message or None."""
        if not self.auto_commit:
            return None
        rels = [self._rel(p) for p in paths]
        try:
            # a pathspec must exist on disk or in the index, else git aborts
            tracked = set(run_git(self.root, "ls-files", "-z", "--", *rels).stdout.split("\0"))
            rels = [r for r in rels if (self.root / r).exists()
                    or r in tracked or any(t.startswith(r + "/") for t in tracked)]
            if not rels:
                return None
            run_git(self.root, "add", "-A", "--", *rels)
            staged = run_git(self.root, "diff", "--cached", "--quiet", "--", *rels, check=False)
            if staged.returncode == 0:
                return None
            run_git(self.root, "commit", "-q", "-m", message, "--", *rels)
        except GitError as e:
            log.warning("auto-commit failed: %s", e)
            return str(e)
        return None

    def _finish(self, paths: list[Path], message: str) -> str | None:
        err = self._commit(paths, message)
        self.record_state()
        return err

    def save_task(self, data: dict[str, Any], create: bool = False) -> tuple[dict[str, Any], str | None]:
        """Create or update a task. Returns (task, commit_error)."""
        with self.lock:
            self.refresh()
            tid = data.get("id") or (self.next_id() if create else None)
            if not tid:
                raise ValidationError("missing task id")
            path = self.task_path(tid)
            if create and path.exists():
                raise ValidationError(f"task {tid} already exists")
            if not create and not path.exists():
                raise NotFound(tid)
            raw = load_file(path) if path.exists() else {}
            raw.update({k: v for k, v in data.items() if k != "id"})
            raw["id"] = tid
            task = clean_task(raw)
            if not task["title"]:
                raise ValidationError("title is required")
            self._check_graph(task)
            raw.update(task)  # keep unknown user fields, canonical known ones
            if "lang" not in task:
                raw.pop("lang", None)
            dump_file(path, raw)
            with self.tx() as db:
                self._upsert_task(db, task)
            err = self._finish([path], f"task: {'create' if create else 'update'} {tid}")
            return self.get_task(tid), err

    def set_status(self, task_id: str, status: str) -> tuple[dict[str, Any], str | None]:
        return self.save_task({"id": task_id, "status": status})

    def delete_task(self, task_id: str) -> str | None:
        import shutil

        with self.lock:
            self.refresh()
            path = self.task_path(task_id)
            if not path.exists():
                raise NotFound(task_id)
            changed = [path]
            # drop the id from tasks that are blocked by it
            for dep in self.get_task(task_id)["blocks"]:
                dpath = self.task_path(dep)
                raw = load_file(dpath)
                raw["blocked_by"] = [b for b in raw.get("blocked_by") or [] if b != task_id]
                dump_file(dpath, raw)
                with self.tx() as db:
                    self._upsert_task(db, clean_task(raw, dep))
                changed.append(dpath)
            path.unlink()
            tdir = self.task_dir(task_id)
            if tdir.is_dir():
                shutil.rmtree(tdir)
                changed.append(tdir)
            with self.tx() as db:
                self._delete_task_rows(db, task_id)
            return self._finish(changed, f"task: delete {task_id}")

    # ---- comments ----------------------------------------------------
    def author_identity(self) -> str:
        if self.author_override:
            return self.author_override
        name = run_git(self.root, "config", "user.name", check=False).stdout.strip()
        email = run_git(self.root, "config", "user.email", check=False).stdout.strip()
        if not name:
            import getpass
            name = getpass.getuser()
        return f"{name} <{email}>" if email else name

    def comment_path(self, task_id: str, cid: str) -> Path:
        if not re.match(r"^[0-9TZ]+-[0-9a-f]+$", cid or ""):
            raise ValidationError("invalid comment id")
        return self.task_dir(task_id) / "comments" / f"{cid}.json5"

    def _attachment_dir(self, task_id: str, cid: str) -> Path:
        self.comment_path(task_id, cid)  # validates cid
        return self.task_dir(task_id) / "attachments" / cid

    def attachment_path(self, task_id: str, cid: str, name: str) -> Path:
        base = self._attachment_dir(task_id, cid).resolve()
        p = (base / name).resolve()
        if p.parent != base or not p.is_file():
            raise NotFound(name)
        return p

    def _store_files(self, task_id: str, cid: str,
                     files: Iterable[tuple[str, bytes]]) -> list[dict[str, Any]]:
        import mimetypes

        out = []
        adir = self._attachment_dir(task_id, cid)
        for filename, content in files:
            if not content and not filename:
                continue
            if len(content) > self.max_attachment_bytes:
                raise ValidationError(f"{filename}: file too large")
            name = sanitize_filename(filename)
            stem, dot, ext = name.rpartition(".")
            if not dot:
                stem, ext = name, ""
            n = 1
            while (adir / name).exists():
                n += 1
                name = f"{stem}-{n}{dot}{ext}"
            adir.mkdir(parents=True, exist_ok=True)
            (adir / name).write_bytes(content)
            out.append({
                "name": name,
                "file": f"attachments/{cid}/{name}",
                "mime": mimetypes.guess_type(name)[0] or "application/octet-stream",
                "size": len(content),
            })
        return out

    def _require_task(self, task_id: str) -> None:
        if not self.task_path(task_id).exists():
            raise NotFound(task_id)

    def add_comment(self, task_id: str, body: str,
                    files: Iterable[tuple[str, bytes]] = ()) -> tuple[dict[str, Any], str | None]:
        import secrets
        from datetime import datetime, timezone

        with self.lock:
            self.refresh()
            self._require_task(task_id)
            body = unicodedata.normalize("NFC", (body or "").strip())
            now = datetime.now(timezone.utc)
            cid = f"{now:%Y%m%dT%H%M%S}{now.microsecond // 1000:03d}Z-{secrets.token_hex(3)}"
            atts = self._store_files(task_id, cid, files)
            if not body and not atts:
                raise ValidationError("comment is empty")
            c = {"id": cid, "author": self.author_identity(),
                 "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "edited_at": None, "body": body, "attachments": atts}
            path = self.comment_path(task_id, cid)
            dump_file(path, c)
            with self.tx() as db:
                self._insert_comment(db, task_id, cid, c)
            paths = [path] + ([self._attachment_dir(task_id, cid)] if atts else [])
            return c, self._finish(paths, f"task: comment on {task_id}")

    def _own_comment(self, task_id: str, cid: str) -> tuple[Path, dict[str, Any]]:
        path = self.comment_path(task_id, cid)
        if not path.is_file():
            raise NotFound(cid)
        c = load_file(path)
        if c.get("author") != self.author_identity():
            raise PermissionError("only the author can change this comment")
        return path, c

    def edit_comment(self, task_id: str, cid: str, body: str,
                     add_files: Iterable[tuple[str, bytes]] = (),
                     remove: Iterable[str] = ()) -> tuple[dict[str, Any], str | None]:
        from datetime import datetime, timezone

        with self.lock:
            self.refresh()
            path, c = self._own_comment(task_id, cid)
            remove = set(remove)
            kept = []
            for a in c.get("attachments") or []:
                if a.get("name") in remove:
                    try:
                        self.attachment_path(task_id, cid, a["name"]).unlink()
                    except NotFound:
                        pass
                else:
                    kept.append(a)
            c["attachments"] = kept + self._store_files(task_id, cid, add_files)
            c["body"] = unicodedata.normalize("NFC", (body or "").strip())
            if not c["body"] and not c["attachments"]:
                raise ValidationError("comment is empty")
            c["edited_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            dump_file(path, c)
            with self.tx() as db:
                self._insert_comment(db, task_id, cid, c)
            return c, self._finish([path, self._attachment_dir(task_id, cid)],
                                   f"task: edit comment on {task_id}")

    def delete_comment(self, task_id: str, cid: str) -> str | None:
        import shutil

        with self.lock:
            self.refresh()
            path, _ = self._own_comment(task_id, cid)
            path.unlink()
            adir = self._attachment_dir(task_id, cid)
            if adir.is_dir():
                shutil.rmtree(adir)
            with self.tx() as db:
                db.execute("DELETE FROM comments WHERE project_path=? AND task_id=? AND id=?",
                           (self.project, task_id, cid))
            return self._finish([path, adir], f"task: delete comment on {task_id}")

    # ---- sync: Update (pull) / Save work (commit + push) -------------
    def _upstream(self) -> str | None:
        p = run_git(self.root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}",
                    check=False)
        return p.stdout.strip() if p.returncode == 0 else None

    def _ahead_behind(self) -> tuple[int, int]:
        p = run_git(self.root, "rev-list", "--left-right", "--count", "HEAD...@{u}", check=False)
        if p.returncode != 0:
            return 0, 0
        a, b = p.stdout.split()
        return int(a), int(b)

    def _unmerged(self) -> list[str]:
        p = run_git(self.root, "diff", "--name-only", "--diff-filter=U", check=False)
        return [x for x in p.stdout.splitlines() if x]

    def sync_status(self) -> dict[str, Any]:
        with self.lock:
            p = run_git(self.root, "symbolic-ref", "--short", "-q", "HEAD", check=False)
            branch = p.stdout.strip() or self.head()[:8]
            upstream = self._upstream()
            ahead, behind = self._ahead_behind() if upstream else (0, 0)
            remotes = run_git(self.root, "remote", check=False).stdout.split()
            return {"branch": branch, "upstream": upstream, "ahead": ahead,
                    "behind": behind, "changes": len(self.dirty_files()),
                    "has_remote": bool(remotes)}

    def pull(self, rebase: bool = False) -> Summary:
        """Fetch + fast-forward (or rebase), then refresh the index from the diff."""
        with self.lock:
            if not self._upstream():
                raise SyncError("no_upstream")
            run_git(self.root, "fetch", "--quiet", timeout=120)
            ahead, behind = self._ahead_behind()
            if behind:
                if ahead and not rebase:
                    raise SyncError("diverged", f"ahead {ahead}, behind {behind}")
                if ahead:
                    p = run_git(self.root, "rebase", "--autostash", "@{u}", check=False)
                    if p.returncode != 0:
                        files = self._unmerged()
                        run_git(self.root, "rebase", "--abort", check=False)
                        raise SyncError("conflict", (p.stderr or p.stdout).strip(), files)
                else:
                    p = run_git(self.root, "merge", "--ff-only", "--autostash", "@{u}",
                                check=False)
                    if p.returncode != 0:
                        raise SyncError("failed", (p.stderr or p.stdout).strip(),
                                        self._unmerged())
                files = self._unmerged()
                if files:  # re-applying local edits conflicted; stash is kept by git
                    raise SyncError("stash_conflict", "", files)
            return self.refresh()

    def save_work(self, message: str | None = None,
                  set_upstream: bool = False) -> dict[str, Any]:
        """Commit only .todo/ changes, then push. Never force-pushes."""
        with self.lock:
            committed = False
            if self.dirty_files():
                run_git(self.root, "add", "-A", "--", TODO_DIR)
                names = run_git(self.root, "diff", "--cached", "--name-only", "--",
                                TASKS_REL).stdout.splitlines()
                ids = {c[1] for c in map(classify_path, names) if c}
                message = (message or "").strip() or f"tasks: update {len(ids)} task(s)"
                run_git(self.root, "commit", "-q", "-m", message, "--", TODO_DIR)
                committed = True
            self.record_state()

            pushed = False
            if not self._upstream():
                if not run_git(self.root, "remote", check=False).stdout.split():
                    raise SyncError("no_remote")
                if not set_upstream:
                    raise SyncError("no_upstream")
                remote = "origin" if "origin" in run_git(
                    self.root, "remote").stdout.split() else run_git(
                    self.root, "remote").stdout.split()[0]
                p = run_git(self.root, "push", "-u", remote, "HEAD", check=False, timeout=120)
                if p.returncode != 0:
                    raise SyncError("push_failed", p.stderr.strip())
                pushed = True
            elif self._ahead_behind()[0]:
                p = run_git(self.root, "push", check=False, timeout=120)
                if p.returncode != 0:
                    rejected = "rejected" in p.stderr or "fetch first" in p.stderr
                    raise SyncError("rejected" if rejected else "push_failed", p.stderr.strip())
                pushed = True
            return {"committed": committed, "pushed": pushed, "message": message}
