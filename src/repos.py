"""Registry of repositories ultra-todo has served, for `ultra-todo ls` / `ultra-todo open NAME`.

Kept as a small JSON file in the user data dir (not the cache, which is disposable).
Repos found in the cache index are imported too, so ones opened before the registry
existed show up.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import platformdirs

from engine import EngineError, default_db_path, fold


def default_registry_path() -> Path:
    return Path(platformdirs.user_data_dir("ultra-todo")) / "repos.json"


def _read(path: Path) -> dict[str, dict]:
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n", "utf-8")
    os.replace(tmp, path)


def _cached_paths(db_path: Path) -> list[str]:
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as conn:
            return [r[0] for r in conn.execute("SELECT project_path FROM projects")]
    except sqlite3.Error:
        return []


def remember(repo: str, registry: Path | None = None) -> None:
    path = registry or default_registry_path()
    data = _read(path)
    data[repo] = {"last_opened": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _write(path, data)


def known(registry: Path | None = None, db_path: Path | None = None) -> list[dict]:
    """All known repos as {name, path, last_opened, exists}, most recently opened first."""
    path = registry or default_registry_path()
    data = _read(path)
    new = [p for p in _cached_paths(db_path or default_db_path()) if p not in data]
    if new:
        for p in new:
            data[p] = {"last_opened": None}
        _write(path, data)
    repos = [{"name": Path(p).name, "path": p, "last_opened": v.get("last_opened"),
              "exists": Path(p).is_dir()} for p, v in data.items()]
    repos.sort(key=lambda r: fold(r["name"]))
    repos.sort(key=lambda r: r["last_opened"] or "", reverse=True)
    return repos


def resolve(name: str, registry: Path | None = None, db_path: Path | None = None) -> str:
    """Path of the known repo called `name` (case-insensitive), or numbered `name` in `ls`."""
    listed = known(registry, db_path)
    repos = [r for r in listed if r["exists"]]
    hits = [r for r in repos if r["name"] == name] or \
           [r for r in repos if fold(r["name"]) == fold(name)]
    if not hits and name.isdigit():
        n = int(name)
        if not 1 <= n <= len(listed):
            raise EngineError(f"no repo number {n} (ultra-todo ls shows 1-{len(listed)})")
        if not listed[n - 1]["exists"]:
            raise EngineError(f"repo {n} is missing: {listed[n - 1]['path']}")
        return listed[n - 1]["path"]
    if len(hits) == 1:
        return hits[0]["path"]
    if hits:
        raise EngineError(f"several repos are called {name!r}; use its number from ls or a path:\n"
                          + "\n".join("  " + r["path"] for r in hits))
    names = ", ".join(sorted({r["name"] for r in repos})) or "none yet"
    raise EngineError(f"no known repo called {name!r} (known: {names})")
