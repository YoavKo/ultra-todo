import json
import subprocess
from pathlib import Path

import pytest


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Tester")
    git(path, "config", "user.email", "tester@example.com")
    git(path, "config", "commit.gpgsign", "false")
    return path


def write_task(repo: Path, tid: str, **fields) -> Path:
    data = {"id": tid, "title": fields.pop("title", tid), "status": "todo", **fields}
    p = repo / ".todo" / "tasks" / f"{tid}.json5"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def commit_all(repo: Path, msg="c"):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)


@pytest.fixture
def repo(tmp_path):
    return init_repo(tmp_path / "proj")


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "cache" / "index.sqlite3"


@pytest.fixture
def make_engine(repo, db_path):
    from engine import Engine

    def _make(**kw):
        return Engine(repo, db_path=db_path, **kw)
    return _make
