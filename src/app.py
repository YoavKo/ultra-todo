"""ultra-todo Flask server.

Run: ``uv run ultra-todo --target /path/to/repo [--lang he|en] [--auto-commit]``
"""
from __future__ import annotations

import argparse
import logging
import re
import secrets
import socket
import sys
from datetime import datetime
from urllib.parse import urlparse

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request,
                   send_file, url_for)
from flask_babel import format_datetime, get_locale, gettext as _, ngettext
from markupsafe import Markup, escape

from engine import (PRIORITIES, STATUSES, CycleError, Engine, EngineError, GitError, NotFound,
                    SyncError, ValidationError)
from i18n import COOKIE, SUPPORTED, init_babel, text_dir
import repos

log = logging.getLogger("ultra-todo")

INLINE_IMAGES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
CSRF_COOKIE = "csrf_token"


def priority_label(priority: str) -> str:
    return {"high": _("High"), "medium": _("Medium"), "low": _("Low")}.get(priority, priority)


def status_label(status: str) -> str:
    return {"todo": _("To do"), "in_progress": _("In progress"),
            "done": _("Done"), "blocked": _("Blocked")}.get(status, status)


def sync_error_message(e: SyncError) -> str:
    return {
        "no_upstream": _("This branch has no upstream. Use “Save work” to publish it first."),
        "no_remote": _("This repository has no remote configured."),
        "diverged": _("Your branch and the remote have diverged. Update with rebase?"),
        "conflict": _("Update stopped: the same files changed on both sides. "
                      "Nothing was changed; resolve these files in git:"),
        "stash_conflict": _("Updated, but re-applying your local edits conflicted. "
                            "Your edits are kept in git stash. Files:"),
        "rejected": _("The remote has newer changes. Click “Update” first, then save again."),
        "push_failed": _("Push failed."),
        "failed": _("Update failed."),
    }.get(e.code, e.code)


_URL_RE = re.compile(r"(https?://[^\s<>\"']+)")


def linkify(text: str) -> Markup:
    """Escape, turn URLs into links and newlines into <br>."""
    out = []
    for i, part in enumerate(_URL_RE.split(text or "")):
        if i % 2:
            u = escape(part)
            out.append(Markup(f'<a href="{u}" rel="noopener noreferrer" target="_blank" dir="ltr">{u}</a>'))
        else:
            out.append(escape(part))
    return Markup("").join(out).replace("\n", Markup("<br>"))


def create_app(engine: Engine, default_lang: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(DEFAULT_LANG=default_lang, ENGINE=engine,
                      MAX_CONTENT_LENGTH=engine.max_attachment_bytes * 5 + 1024 * 1024,
                      SECRET_KEY=secrets.token_hex(32))
    init_babel(app)
    app.jinja_env.filters["linkify"] = linkify
    # globals (unlike context vars) are visible inside imported macros
    app.jinja_env.globals.update(priority_label=priority_label, status_label=status_label,
                                 PRIORITIES=PRIORITIES, STATUSES=STATUSES)

    @app.template_filter("dt")
    def _dt(value):
        if not value:
            return ""
        try:
            d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return value
        return format_datetime(d.astimezone(), "medium")

    @app.context_processor
    def _ctx():
        loc = str(get_locale())
        return {"locale": loc, "dir": text_dir(loc), "status_label": status_label,
                "STATUSES": STATUSES, "PRIORITIES": PRIORITIES,
                "priority_label": priority_label, "project_name": engine.root.name,
                "project_path": engine.project, "csrf_token": request.cookies.get(CSRF_COOKIE)
                or getattr(request, "_new_csrf", ""), "SUPPORTED": SUPPORTED,
                "ngettext": ngettext}

    # ---- CSRF (double-submit cookie) ---------------------------------
    @app.before_request
    def _csrf():
        if request.method == "POST":
            sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
            if not sent or sent != request.cookies.get(CSRF_COOKIE):
                abort(403, _("Invalid or missing CSRF token. Reload the page."))
        elif not request.cookies.get(CSRF_COOKIE):
            request._new_csrf = secrets.token_urlsafe(32)

    @app.after_request
    def _set_csrf(resp):
        tok = getattr(request, "_new_csrf", None)
        if tok:
            resp.set_cookie(CSRF_COOKIE, tok, samesite="Strict", httponly=False)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        return resp

    # ---- error handling ----------------------------------------------
    def wants_json() -> bool:
        return (request.is_json or request.path.startswith(("/api/", "/sync/"))
                or request.headers.get("Accept", "").startswith("application/json"))

    def error_text(e: Exception) -> str:
        if isinstance(e, CycleError):
            return _("Dependency cycle: %(cycle)s", cycle=" → ".join(e.cycle))
        if isinstance(e, PermissionError):
            return _("Only the author can change this comment.")
        return str(e)

    @app.errorhandler(ValidationError)
    @app.errorhandler(PermissionError)
    @app.errorhandler(GitError)
    def _bad(e):
        code = 403 if isinstance(e, PermissionError) else 400
        msg = error_text(e)
        if wants_json():
            body = {"error": msg}
            if isinstance(e, CycleError):
                body["cycle"] = e.cycle
            return jsonify(body), code
        return render_template("error.html", message=msg, code=code), code

    @app.errorhandler(NotFound)
    def _nf(e):
        if wants_json():
            return jsonify({"error": _("Not found")}), 404
        return render_template("error.html", message=_("Not found: %(what)s", what=str(e)),
                               code=404), 404

    @app.errorhandler(SyncError)
    def _sync_err(e):
        return jsonify({"error": sync_error_message(e), "code": e.code,
                        "detail": e.detail, "files": e.files}), 409

    # ---- helpers -----------------------------------------------------
    def filters():
        return {
            "q": request.args.get("q", "").strip(),
            "assignee": request.args.get("assignee", "").strip(),
            "tags": [t for t in request.args.getlist("tag") if t.strip()],
            "match": "any" if request.args.get("match") == "any" else "all",
            "priority": request.args.get("priority") if request.args.get("priority")
            in PRIORITIES else "",
        }

    def form_task(form) -> dict:
        data = {k: form.get(k, "") for k in ("title", "status", "assignee", "description")}
        if form.get("priority"):
            data["priority"] = form["priority"]
        data["tags"] = form.getlist("tags") + form.get("tags_text", "").split(",")
        if "blocked_by_field" in form:  # only the new-task form edits blockers inline
            data["blocked_by"] = [b.strip() for b in form.getlist("blocked_by")
                                  for b in b.replace(",", " ").split() if b.strip()]
        if form.get("lang") in SUPPORTED:
            data["lang"] = form["lang"]
        elif "lang" in form:
            data["lang"] = None
        return data

    def uploads():
        return [(f.filename or "file", f.read()) for f in request.files.getlist("files")
                if f and f.filename]

    def back(default="index"):
        ref = request.referrer
        if ref and urlparse(ref).netloc == request.host:
            return redirect(ref)
        return redirect(url_for(default))

    def flash_commit(err):
        if err:
            flash(_("Saved, but auto-commit failed: %(err)s", err=err), "warning")

    # ---- pages -------------------------------------------------------
    @app.get("/")
    def index():
        engine.refresh()
        f = filters()
        tasks = engine.list_tasks(q=f["q"], assignee=f["assignee"], tags=f["tags"], priority=f["priority"] or None,
                                  match=f["match"])
        columns = {s: [t for t in tasks if t["status"] == s] for s in STATUSES}
        return render_template("board.html", columns=columns, f=f, view="board",
                               all_tags=engine.all_tags(), assignees=engine.assignees(),
                               total=len(tasks), warnings=engine.warnings)

    @app.get("/list")
    def list_view():
        engine.refresh()
        f = filters()
        tasks = engine.list_tasks(q=f["q"], assignee=f["assignee"], tags=f["tags"], priority=f["priority"] or None,
                                  match=f["match"])
        return render_template("list.html", tasks=tasks, f=f, view="list",
                               all_tags=engine.all_tags(), assignees=engine.assignees(),
                               total=len(tasks), warnings=engine.warnings)

    @app.get("/task/new")
    def new_task():
        engine.refresh()
        task = {"id": "", "title": "", "status": request.args.get("status", "todo"),
                "priority": "medium",
                "assignee": "", "description": "", "tags": [], "blocked_by": []}
        return render_template("task.html", task=task, is_new=True, comments=[],
                               candidates=engine.list_tasks(),
                               all_tags=engine.all_tags())

    @app.post("/task")
    def create_task():
        data = form_task(request.form)
        if request.form.get("id", "").strip():
            data["id"] = request.form["id"].strip()
        task, err = engine.save_task(data, create=True)
        flash_commit(err)
        flash(_("Task %(id)s created.", id=task["id"]), "success")
        return redirect(url_for("task_view", task_id=task["id"]))

    @app.get("/task/<task_id>")
    def task_view(task_id):
        engine.refresh()
        task = engine.get_task(task_id)
        others = [t for t in engine.list_tasks() if t["id"] != task_id]
        by_id = {t["id"]: t for t in others}
        return render_template("task.html", task=task, is_new=False,
                               comments=engine.get_comments(task_id),
                               candidates=others, by_id=by_id,
                               blockers=[(b, by_id.get(b)) for b in task["blocked_by"]],
                               blocking=[(b, by_id.get(b)) for b in task["blocks"]],
                               all_tags=engine.all_tags(), me=engine.author_identity(),
                               inline_images=INLINE_IMAGES)

    @app.post("/task/<task_id>")
    def update_task(task_id):
        data = form_task(request.form)
        data["id"] = task_id
        _task, err = engine.save_task(data)
        flash_commit(err)
        flash(_("Task saved."), "success")
        return redirect(url_for("task_view", task_id=task_id))

    @app.post("/task/<task_id>/dependency")
    def add_dependency(task_id):
        other = request.form.get("other", "").strip()
        if not other:
            return redirect(url_for("task_view", task_id=task_id) + "#deps")
        if request.form.get("kind") == "blocks":  # this task blocks `other`
            err = engine.add_dependency(other, task_id)
        else:
            err = engine.add_dependency(task_id, other)
        flash_commit(err)
        flash(_("Dependency added."), "success")
        return redirect(url_for("task_view", task_id=task_id) + "#deps")

    @app.post("/task/<task_id>/blocked_by/<blocker_id>/delete")
    def remove_dependency(task_id, blocker_id):
        """Remove the edge task_id -> blocker_id; return to the page it came from."""
        flash_commit(engine.remove_dependency(task_id, blocker_id))
        flash(_("Dependency removed."), "success")
        here = request.form.get("from") or task_id
        return redirect(url_for("task_view", task_id=here) + "#deps")

    @app.post("/task/<task_id>/delete")
    def delete_task(task_id):
        flash_commit(engine.delete_task(task_id))
        flash(_("Task %(id)s deleted.", id=task_id), "success")
        return redirect(url_for("index"))

    @app.post("/task/<task_id>/status")
    def set_status(task_id):
        status = (request.get_json(silent=True) or {}).get("status") or request.form.get("status")
        task, err = engine.set_status(task_id, status)
        return jsonify({"task": task, "commit_error": err})

    # ---- comments ----------------------------------------------------
    @app.post("/task/<task_id>/comment")
    def add_comment(task_id):
        _c, err = engine.add_comment(task_id, request.form.get("body", ""), uploads())
        flash_commit(err)
        return redirect(url_for("task_view", task_id=task_id) + "#comments")

    @app.post("/task/<task_id>/comment/<cid>")
    def edit_comment(task_id, cid):
        _c, err = engine.edit_comment(task_id, cid, request.form.get("body", ""), uploads(),
                                      request.form.getlist("remove"))
        flash_commit(err)
        return redirect(url_for("task_view", task_id=task_id) + f"#c-{cid}")

    @app.post("/task/<task_id>/comment/<cid>/delete")
    def delete_comment(task_id, cid):
        flash_commit(engine.delete_comment(task_id, cid))
        return redirect(url_for("task_view", task_id=task_id) + "#comments")

    @app.get("/task/<task_id>/attachment/<cid>/<path:name>")
    def attachment(task_id, cid, name):
        path = engine.attachment_path(task_id, cid, name)
        import mimetypes
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        inline = mime in INLINE_IMAGES and not request.args.get("download")
        resp = send_file(path, mimetype=mime if inline else "application/octet-stream",
                         as_attachment=not inline, download_name=path.name)
        resp.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return resp

    # ---- language ----------------------------------------------------
    @app.post("/lang")
    def set_lang():
        lang = request.form.get("lang")
        resp = back()
        if lang in SUPPORTED:
            resp.set_cookie(COOKIE, lang, max_age=3600 * 24 * 365, samesite="Lax")
        return resp

    # ---- JSON API ----------------------------------------------------
    @app.get("/api/tags")
    def api_tags():
        engine.refresh()
        return jsonify(engine.all_tags())

    @app.get("/api/graph")
    def api_graph():
        engine.refresh()
        return jsonify({"nodes": [{"id": t["id"], "title": t["title"], "status": t["status"]}
                                  for t in engine.list_tasks()],
                        "edges": [{"task": k, "blocked_by": b}
                                  for k, v in engine.dependency_graph().items() for b in v]})

    @app.get("/api/sync-status")
    def api_sync_status():
        return jsonify(engine.sync_status())

    @app.post("/sync/pull")
    def sync_pull():
        rebase = bool((request.get_json(silent=True) or {}).get("rebase"))
        s = engine.pull(rebase=rebase)
        if s["full"]:
            msg = _("Updated. Index rebuilt.")
        elif not s.changed:
            msg = _("Already up to date.")
        else:
            msg = _("Updated: %(a)d added, %(u)d changed, %(d)d removed, %(c)d comments.",
                    a=s["tasks_added"], u=s["tasks_updated"], d=s["tasks_deleted"],
                    c=s["comments_changed"])
        return jsonify({"message": msg, "summary": dict(s), "status": engine.sync_status()})

    @app.post("/sync/save")
    def sync_save():
        body = request.get_json(silent=True) or {}
        res = engine.save_work(body.get("message"), bool(body.get("set_upstream")))
        if res["committed"] and res["pushed"]:
            msg = _("Committed and pushed.")
        elif res["pushed"]:
            msg = _("Pushed.")
        elif res["committed"]:
            msg = _("Committed.")
        else:
            msg = _("Nothing to save.")
        return jsonify({"message": msg, **res, "status": engine.sync_status()})

    return app


DEFAULT_PORT = 8080


def bind_socket(host: str, port: int | None, tries: int = 50) -> socket.socket:
    """Bind a listening socket. With no explicit port, take the first free one from 8080 up,
    so several instances (one per repo) can run side by side."""
    ports = [port] if port is not None else range(DEFAULT_PORT, DEFAULT_PORT + tries)
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    err = None
    for p in ports:
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.bind((host, p))
            return sock
        except OSError as e:
            sock.close()
            err = e
    if port is not None:
        raise SystemExit(f"error: port {port} on {host} is in use ({err.strerror}); "
                         "pick another with --port or omit it to choose a free one")
    raise SystemExit(f"error: no free port in {DEFAULT_PORT}-{DEFAULT_PORT + tries - 1}")


def cmd_ls(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(prog="ultra-todo ls", description="List known repositories")
    ap.add_argument("--names", action="store_true", help="names only (for shell completion)")
    ap.add_argument("--all", action="store_true", help="with --names: include missing repos")
    args = ap.parse_args(argv)
    known = repos.known()
    if args.names:
        print("\n".join(sorted({r["name"] for r in known if r["exists"] or args.all})))
        return
    if not known:
        print("no repos yet; run: ultra-todo --target PATH")
        return
    width = max(len(r["name"]) for r in known)
    num = len(str(len(known)))
    for i, r in enumerate(known, 1):
        when = (datetime.fromisoformat(r["last_opened"]).astimezone().strftime("%Y-%m-%d %H:%M")
                if r["last_opened"] else "")
        note = "" if r["exists"] else "  (missing)"
        print(f"{i:>{num}}  {when or '-':<16}  {r['name']:<{width}}  {r['path']}{note}")


def cmd_forget(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(
        prog="ultra-todo forget",
        description="Remove repos from the known list (their files are not touched)")
    ap.add_argument("repos", nargs="+", metavar="NAME|NUMBER",
                    help="names or ls numbers; comma-separated lists work too (3,5)")
    args = ap.parse_args(argv)
    names = [n.strip() for a in args.repos for n in a.split(",") if n.strip()]
    try:
        removed = repos.forget(names)
    except EngineError as e:
        raise SystemExit(f"error: {e}")
    for r in removed:
        print(f"forgot {r['name']}  {r['path']}")


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["ls"]:
        return cmd_ls(argv[1:])
    if argv[:1] == ["forget"]:
        return cmd_forget(argv[1:])
    if argv[:1] == ["open"]:
        if len(argv) < 2 or argv[1].startswith("-"):
            raise SystemExit("usage: ultra-todo open NAME|NUMBER [options]  (see: ultra-todo ls)")
        try:
            argv = ["--target", repos.resolve(argv[1])] + argv[2:]
        except EngineError as e:
            raise SystemExit(f"error: {e}")
    ap = argparse.ArgumentParser(
        prog="ultra-todo", description="Local GitOps task manager",
        epilog="also: 'ultra-todo ls' lists known repos, 'ultra-todo open NAME|NUMBER' opens one, 'ultra-todo forget NAME|NUMBER,...' removes some")
    ap.add_argument("--target", required=True, help="path of the git repository to manage")
    ap.add_argument("--lang", choices=SUPPORTED, help="default UI language")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int,
                    help=f"port (default: first free from {DEFAULT_PORT})")
    ap.add_argument("--auto-commit", action="store_true", help="commit every change")
    ap.add_argument("--author", help="override comment author (default: git config user.name)")
    ap.add_argument("--max-attachment-mb", type=float, default=10)
    ap.add_argument("--db", help="override the SQLite index path")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(levelname)s %(message)s")
    try:
        engine = Engine(args.target, db_path=args.db, auto_commit=args.auto_commit,
                        author=args.author,
                        max_attachment_bytes=int(args.max_attachment_mb * 1024 * 1024))
        s = engine.refresh()
    except EngineError as e:
        raise SystemExit(f"error: {e}")
    log.info("%s: %s", engine.project, "full scan" if s["full"]
             else ("refreshed" if s.changed else "cache hit"))
    if args.db is None:
        repos.remember(engine.project)
    app = create_app(engine, args.lang)
    sock = bind_socket(args.host, args.port)
    port = sock.getsockname()[1]
    log.info("serving %s on http://%s:%d/", engine.project, args.host, port)
    if args.debug:
        sock.close()  # the reloader binds its own socket
        app.run(args.host, port, debug=True)
    else:
        from waitress import serve
        sock.listen(1024)
        serve(app, sockets=[sock], threads=args.threads)


if __name__ == "__main__":
    main()
