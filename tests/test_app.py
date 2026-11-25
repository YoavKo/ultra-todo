import re

import pytest

from conftest import commit_all, write_task
from app import create_app


@pytest.fixture
def client(repo, make_engine):
    write_task(repo, "TKT-101", title="שלום עולם", tags=["ui", "דחוף"])
    write_task(repo, "TKT-102", title="Second", tags=["api"], blocked_by=["TKT-101"])
    commit_all(repo)
    eng = make_engine()
    eng.refresh()
    app = create_app(eng)
    app.config["TESTING"] = True
    c = app.test_client()
    c.get("/")  # obtain CSRF cookie
    c.token = c.get_cookie("csrf_token").value
    c.eng = eng
    return c


def post(c, url, **kw):
    data = kw.pop("data", {})
    data.setdefault("csrf_token", c.token)
    return c.post(url, data=data, **kw)


def test_board_and_tag_filter(client):
    html = client.get("/").get_data(as_text=True)
    assert "שלום עולם" in html and "Second" in html
    html = client.get("/?tag=דחוף").get_data(as_text=True)
    assert "שלום עולם" in html and "Second" not in html
    html = client.get("/list?tag=ui&tag=api&match=any").get_data(as_text=True)
    assert "שלום עולם" in html and "Second" in html


def test_csrf_required(client):
    assert client.post("/task", data={"title": "x"}).status_code == 403


def test_create_update_and_cycle(client):
    r = post(client, "/task", data={"title": "New one", "tags_text": "a, b", "status": "todo"})
    assert r.status_code == 302
    assert client.eng.get_task("TKT-103")["tags"] == ["a", "b"]
    r = post(client, "/task/TKT-101/dependency", data={"kind": "blocked_by", "other": "TKT-102"})
    assert r.status_code == 400 and "TKT-101" in r.get_data(as_text=True)
    r = client.post("/task/TKT-101/status", json={"status": "done"},
                    headers={"X-CSRF-Token": client.token})
    assert r.get_json()["task"]["status"] == "done"


def test_comment_with_attachment(client):
    import io
    r = post(client, "/task/TKT-101/comment", content_type="multipart/form-data",
             data={"body": "see https://example.com\nline2", "csrf_token": client.token,
                   "files": [(io.BytesIO(b"\x89PNG"), "shot.png"), (io.BytesIO(b"<svg/>"), "x.svg")]})
    assert r.status_code == 302
    html = client.get("/task/TKT-101").get_data(as_text=True)
    assert 'href="https://example.com"' in html and "<br />" in html  # bare URLs link, newlines break
    cid = client.eng.get_comments("TKT-101")[0]["id"]
    png = client.get(f"/task/TKT-101/attachment/{cid}/shot.png")
    assert png.mimetype == "image/png" and "attachment" not in png.headers.get("Content-Disposition", "")
    svg = client.get(f"/task/TKT-101/attachment/{cid}/x.svg")
    assert svg.mimetype == "application/octet-stream" and "attachment" in svg.headers["Content-Disposition"]
    assert client.get(f"/task/TKT-101/attachment/{cid}/..%2F..%2FTKT-101.json5").status_code == 404


def test_language_and_direction(client):
    html = client.get("/", headers={"Accept-Language": "he"}).get_data(as_text=True)
    assert re.search(r'<html lang="he" dir="rtl">', html)
    assert "לוח" in html and "שמירת עבודה" in html
    html = client.get("/?lang=en", headers={"Accept-Language": "he"}).get_data(as_text=True)
    assert '<html lang="en" dir="ltr">' in html
    post(client, "/lang", data={"lang": "he"})
    html = client.get("/").get_data(as_text=True)
    assert 'dir="rtl"' in html


def test_sync_endpoints_without_remote(client):
    s = client.get("/api/sync-status").get_json()
    assert s["branch"] == "main" and s["upstream"] is None
    r = client.post("/sync/pull", json={}, headers={"X-CSRF-Token": client.token})
    assert r.status_code == 409 and r.get_json()["code"] == "no_upstream"


def test_api_tags_counts(client):
    assert {t["tag"]: t["count"] for t in client.get("/api/tags").get_json()} == \
        {"ui": 1, "דחוף": 1, "api": 1}


def test_dependencies_shown_both_ways_and_removable(client):
    # TKT-102 is blocked by TKT-101
    html = client.get("/task/TKT-102").get_data(as_text=True)
    deps = re.search(r'<section class="deps".*?</section>', html, re.S).group(0)
    assert "TKT-101" in deps and "שלום עולם" in deps
    assert "/task/TKT-102/blocked_by/TKT-101/delete" in deps
    assert "Waiting on 1 open task" in html
    html = client.get("/task/TKT-101").get_data(as_text=True)
    deps = re.search(r'<section class="deps".*?</section>', html, re.S).group(0)
    assert "TKT-102" in deps and "/task/TKT-102/blocked_by/TKT-101/delete" in deps
    # board card shows both directions
    board = client.get("/").get_data(as_text=True)
    assert "⛔ 1" in board and "➜ 1" in board

    # remove from the *blocking* task's page; we land back on it
    r = post(client, "/task/TKT-102/blocked_by/TKT-101/delete", data={"from": "TKT-101"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/task/TKT-101#deps")
    assert client.eng.get_task("TKT-102")["blocked_by"] == []
    assert client.eng.get_task("TKT-101")["blocks"] == []


def test_add_dependency_both_directions_and_cycle(client):
    post(client, "/task", data={"id": "C", "title": "c", "status": "todo"})
    r = post(client, "/task/C/dependency", data={"kind": "blocked_by", "other": "TKT-102"})
    assert r.status_code == 302 and client.eng.get_task("C")["blocked_by"] == ["TKT-102"]
    # C blocks 101 would close the loop 101 -> C -> 102 -> 101
    r = post(client, "/task/C/dependency", data={"kind": "blocks", "other": "TKT-101"})
    assert r.status_code == 400
    assert client.eng.get_task("TKT-101")["blocked_by"] == []
    r = post(client, "/task/TKT-101/dependency", data={"kind": "blocks", "other": "C"})
    assert r.status_code == 302 and "TKT-101" in client.eng.get_task("C")["blocked_by"]


def test_editing_task_keeps_blockers(client):
    # the edit form no longer carries blockers; saving it must not clear them
    r = post(client, "/task/TKT-102", data={"title": "Second!", "status": "todo"})
    assert r.status_code == 302
    assert client.eng.get_task("TKT-102")["blocked_by"] == ["TKT-101"]


def test_remove_dangling_blocker(client, repo):
    import json
    p = repo / ".todo/tasks/TKT-102/main.json5"
    d = json.loads(p.read_text()); d["blocked_by"] = ["TKT-101"]; p.write_text(json.dumps(d))
    (repo / ".todo/tasks/TKT-101/main.json5").unlink()
    html = client.get("/task/TKT-102").get_data(as_text=True)
    assert "/task/TKT-102/blocked_by/TKT-101/delete" in html


def test_dependencies_in_hebrew(client):
    html = client.get("/task/TKT-102?lang=he").get_data(as_text=True)
    assert "חסום ע״י" in html and "ממתינה למשימה פתוחה אחת" in html and "הסרת הקישור" in html


def test_priority_form_filter_and_hebrew(client):
    r = post(client, "/task/TKT-102", data={"title": "Second", "status": "todo", "priority": "high"})
    assert r.status_code == 302 and client.eng.get_task("TKT-102")["priority"] == "high"
    html = client.get("/list?priority=high").get_data(as_text=True)
    assert "Second" in html and "שלום עולם" not in html and "prio-high" in html
    board = client.get("/?lang=he").get_data(as_text=True)
    assert "גבוהה" in board and "עדיפות" in board
    # board column lists high priority first
    post(client, "/task", data={"id": "Z", "title": "zz", "status": "todo", "priority": "low"})
    post(client, "/task", data={"id": "A0", "title": "aa", "status": "todo", "priority": "high"})
    todo = re.search(r'data-status="todo".*?</section>', client.get("/").get_data(as_text=True), re.S).group(0)
    order = re.findall(r'data-id="([^"]+)"', todo)
    assert order.index("A0") < order.index("Z")


def test_bind_socket_picks_next_free_port():
    import socket
    from app import bind_socket
    first = bind_socket("127.0.0.1", None)
    second = bind_socket("127.0.0.1", None)
    try:
        assert second.getsockname()[1] > first.getsockname()[1]
        with pytest.raises(SystemExit, match="in use"):
            bind_socket("127.0.0.1", first.getsockname()[1])
    finally:
        first.close(); second.close()


def test_description_attachments_in_the_task_form(client):
    import io
    r = post(client, "/task/TKT-101", content_type="multipart/form-data",
             data={"title": "שלום עולם", "status": "todo", "description": "see file",
                   "csrf_token": client.token,
                   "files": [(io.BytesIO(b"\x89PNG"), "shot.png"), (io.BytesIO(b"<svg/>"), "x.svg")]})
    assert r.status_code == 302
    html = client.get("/task/TKT-101").get_data(as_text=True)
    assert "/task/TKT-101/attachment/description/shot.png" in html and "x.svg" in html
    png = client.get("/task/TKT-101/attachment/description/shot.png")
    assert png.mimetype == "image/png"
    svg = client.get("/task/TKT-101/attachment/description/x.svg")
    assert svg.mimetype == "application/octet-stream" and "attachment" in svg.headers["Content-Disposition"]
    assert client.get("/task/TKT-101/attachment/description/..%2Fmain.json5").status_code == 404
    post(client, "/task/TKT-101", content_type="multipart/form-data",
         data={"title": "שלום עולם", "status": "todo", "csrf_token": client.token, "remove": "x.svg"})
    assert [a["name"] for a in client.eng.get_task("TKT-101")["attachments"]] == ["shot.png"]
    r = post(client, "/task", content_type="multipart/form-data",
             data={"title": "n", "status": "todo", "id": "NEW-9", "csrf_token": client.token,
                   "files": [(io.BytesIO(b"hi"), "n.txt")]})
    assert r.status_code == 302
    assert [a["name"] for a in client.eng.get_task("NEW-9")["attachments"]] == ["n.txt"]


@pytest.fixture
def local_client(repo, make_engine, monkeypatch):
    import cli
    write_task(repo, "TKT-101", title="T")
    commit_all(repo)
    eng = make_engine()
    eng.refresh()
    eng.save_task({"id": "TKT-101"}, files=[("log.txt", b"boom")])
    c, _ = eng.add_comment("TKT-101", "see", files=[("shot.png", b"\x89PNG")])
    app = create_app(eng, can_open=True)
    app.config["TESTING"] = True
    cl = app.test_client()
    cl.get("/")
    cl.token = cl.get_cookie("csrf_token").value
    cl.eng, cl.cid, cl.opened = eng, c["id"], []
    monkeypatch.setattr(cli, "launch", lambda path: cl.opened.append(path))
    return cl


def test_open_button_and_endpoint(local_client):
    cl = local_client
    html = cl.get("/task/TKT-101").get_data(as_text=True)
    # the description's file (in its view and in its edit form) and the comment's
    assert html.count("data-open-attachment") == 3
    assert "/task/TKT-101/open-attachment/description/log.txt" in html
    r = cl.post("/task/TKT-101/open-attachment/description/log.txt", headers={"X-CSRF-Token": cl.token})
    assert r.status_code == 200 and "log.txt" in r.get_json()["message"]
    r = cl.post(f"/task/TKT-101/open-attachment/{cl.cid}/shot.png", headers={"X-CSRF-Token": cl.token})
    assert r.status_code == 200
    assert [p.name for p in cl.opened] == ["log.txt", "shot.png"]
    assert cl.post("/task/TKT-101/open-attachment/description/log.txt").status_code == 403  # no CSRF token
    bad = cl.post("/task/TKT-101/open-attachment/description/..%2Fmain.json5", headers={"X-CSRF-Token": cl.token})
    assert bad.status_code == 404 and len(cl.opened) == 2
    r = cl.post("/task/TKT-101/open-attachment/description/log.txt", headers={"X-CSRF-Token": cl.token},
                environ_base={"REMOTE_ADDR": "10.1.2.3"})
    assert r.status_code == 403 and len(cl.opened) == 2  # only requests from this machine


def test_open_disabled_by_default(client):
    import io
    post(client, "/task/TKT-101", content_type="multipart/form-data",
         data={"title": "x", "status": "todo", "csrf_token": client.token,
               "files": [(io.BytesIO(b"1"), "a.txt")]})
    assert "data-open-attachment" not in client.get("/task/TKT-101").get_data(as_text=True)
    r = client.post("/task/TKT-101/open-attachment/description/a.txt", headers={"X-CSRF-Token": client.token})
    assert r.status_code == 403


def test_open_reports_a_missing_opener(local_client, monkeypatch):
    import cli
    def boom(path):
        raise cli.OpenError("cannot open files here: xdg-open not found")
    monkeypatch.setattr(cli, "launch", boom)
    r = local_client.post("/task/TKT-101/open-attachment/description/log.txt",
                          headers={"X-CSRF-Token": local_client.token})
    assert r.status_code == 501 and "xdg-open" in r.get_json()["error"]


# ---- `open`: serve a repo and open the browser ----------------------------------------------
@pytest.fixture
def served(repo, tmp_path, monkeypatch):
    """Run app.main with the server and the browser stubbed out; returns what happened."""
    import app
    import waitress
    write_task(repo, "TKT-101", title="x")
    commit_all(repo)
    seen = {"opened": [], "served": []}
    monkeypatch.setattr(waitress, "serve", lambda a, sockets, threads: seen["served"].append(sockets[0].getsockname()))
    monkeypatch.setattr("webbrowser.open", lambda url: seen["opened"].append(url) or True)
    monkeypatch.setenv("ULTRA_TODO_PROG", "utd")

    def run(*argv):
        seen["opened"].clear()
        seen["served"].clear()
        app.main([*argv, "--db", str(tmp_path / "idx.sqlite3")])
        return seen
    run.repo = repo
    return run


def test_open_serves_and_opens_the_browser(served, repo):
    seen = served("open", str(repo))
    assert len(seen["served"]) == 1
    assert seen["opened"] == [f"http://127.0.0.1:{seen['served'][0][1]}/"]


def test_open_without_a_repo_uses_the_current_one(served, repo, monkeypatch):
    monkeypatch.chdir(repo)
    assert len(served("open")["opened"]) == 1
    assert len(served("open", ".")["opened"]) == 1
    monkeypatch.chdir(repo / ".todo")  # a subdirectory counts
    assert len(served("open")["opened"]) == 1


def test_open_outside_a_repo_asks_which(served, tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    with pytest.raises(SystemExit, match="say which one: utd open NAME"):
        served("open")
    with pytest.raises(SystemExit, match="not a directory"):
        served("open", "./nope")
    with pytest.raises(SystemExit, match="no known repo"):
        served("open", "definitely-not-a-repo")


def test_open_flags(served, repo):
    assert served("open", str(repo), "--no-browser")["opened"] == []   # served, browser closed
    assert len(served("open", str(repo), "--no-browser")["served"]) == 1
    assert len(served("open", str(repo), "--lang", "he", "--port", "0")["opened"]) == 1


def test_repo_open_and_serve_forms(served, repo):
    assert len(served(str(repo), "open")["opened"]) == 1               # utd REPO open
    assert served(str(repo), "serve")["opened"] == []                  # serve alone stays quiet
    assert len(served(str(repo), "serve", "--browser")["opened"]) == 1  # ...unless asked
    assert served(str(repo), "open", "--no-browser")["opened"] == []


def test_a_browser_that_cannot_start_does_not_stop_the_server(served, monkeypatch, repo):
    monkeypatch.setattr("webbrowser.open", lambda url: (_ for _ in ()).throw(RuntimeError("no display")))
    assert len(served("open", str(repo))["served"]) == 1
    monkeypatch.setattr("webbrowser.open", lambda url: False)
    assert len(served("open", str(repo))["served"]) == 1


def test_open_help_and_hosts(served, repo, capsys, monkeypatch):
    import app
    served("open", "-h")
    assert "[REPO]" in capsys.readouterr().out
    seen = served("open", str(repo), "--host", "0.0.0.0")  # a wildcard bind opens the loopback address
    assert seen["opened"][0].startswith("http://127.0.0.1:")


def test_run_returns_open_and_serve_arguments(repo, db_path):
    import cli
    assert cli.run([str(repo), "open", "--port", "9"], "utd", db_path=db_path) == [
        "--target", str(repo), "--browser", "--port", "9"]
    assert cli.run([str(repo), "serve", "--port", "9"], "utd", db_path=db_path) == [
        "--target", str(repo), "--port", "9"]


# ---- Markdown, and editing the description in place ------------------------------------------
@pytest.fixture
def md_client(client):
    client.eng.save_task({"id": "TKT-101", "description": "# Plan\n\n- [x] one\n- two\n\n**bold** and `code`\n\nשלום **עולם**"})
    return client


def test_description_is_rendered_as_markdown_with_an_edit_button(md_client):
    html = md_client.get("/task/TKT-101").get_data(as_text=True)
    view = html[html.index('id="description"'):]
    assert "<h1 dir=\"auto\">Plan</h1>" in view and "<strong>bold</strong>" in view
    assert "<code>code</code>" in view and 'type="checkbox"' in view and "<strong>עולם</strong>" in view
    assert "data-desc-edit" in view and "data-desc-cancel" in view
    assert "<textarea name=\"description\"" in view and "# Plan" in view   # the source, in the (hidden) editor
    assert 'class="stack desc-edit" hidden' in view.replace("\n", " ").replace("  ", " ")  # starts in view mode


def test_saving_the_description_updates_only_the_description(md_client):
    r = post(md_client, "/task/TKT-101/description", data={"csrf_token": md_client.token, "description": "new *text*"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/task/TKT-101#description")
    t = md_client.eng.get_task("TKT-101")
    assert t["description"] == "new *text*" and t["title"] == "שלום עולם" and t["tags"] == ["ui", "דחוף"]
    assert "<em>text</em>" in md_client.get("/task/TKT-101").get_data(as_text=True)
    post(md_client, "/task/TKT-101/description", data={"csrf_token": md_client.token, "description": ""})
    assert md_client.eng.get_task("TKT-101")["description"] == ""
    assert "No description yet." in md_client.get("/task/TKT-101").get_data(as_text=True)
    assert md_client.post("/task/TKT-101/description", data={"description": "x"}).status_code == 403  # no CSRF token


def test_the_main_form_no_longer_touches_the_description(md_client):
    before = md_client.eng.get_task("TKT-101")["description"]
    html = md_client.get("/task/TKT-101").get_data(as_text=True)
    main_form = html[html.index('id="task-form"'): html.index('id="description"')]
    assert 'name="description"' not in main_form      # the textarea lives in the description section
    post(md_client, "/task/TKT-101", data={"csrf_token": md_client.token, "title": "Renamed", "status": "done"})
    t = md_client.eng.get_task("TKT-101")
    assert t["title"] == "Renamed" and t["status"] == "done" and t["description"] == before  # not wiped


def test_new_task_form_still_has_a_description_box(client):
    html = client.get("/task/new").get_data(as_text=True)
    assert 'name="description"' in html and "Markdown is supported" in html
    r = post(client, "/task", data={"csrf_token": client.token, "title": "n", "status": "todo", "id": "MD-1",
                                    "description": "**hi**"})
    assert r.status_code == 302 and client.eng.get_task("MD-1")["description"] == "**hi**"


def test_description_attachments_through_the_description_form(md_client):
    import io
    post(md_client, "/task/TKT-101/description", content_type="multipart/form-data",
         data={"csrf_token": md_client.token, "description": "see ![shot](shot.png) and [log](log.txt)",
               "files": [(io.BytesIO(b"\x89PNG"), "shot.png"), (io.BytesIO(b"boom"), "log.txt")]})
    html = md_client.get("/task/TKT-101").get_data(as_text=True)
    assert '<img src="/task/TKT-101/attachment/description/shot.png" alt="shot"' in html   # file names resolve
    assert '<a href="/task/TKT-101/attachment/description/log.txt">log</a>' in html
    post(md_client, "/task/TKT-101/description", content_type="multipart/form-data",
         data={"csrf_token": md_client.token, "description": "x", "remove": "log.txt"})
    assert [a["name"] for a in md_client.eng.get_task("TKT-101")["attachments"]] == ["shot.png"]


def test_comments_render_markdown_and_never_raw_html(client):
    body = ("**bold** <script>alert(1)</script> <img src=x onerror=alert(2)> [bad](javascript:alert(3)) "
            "![x](data:text/html;base64,AAAA)\n\n> quote\n\n```\n<b>code</b>\n```")
    post(client, "/task/TKT-101/comment", data={"body": body, "csrf_token": client.token})
    html = client.get("/task/TKT-101").get_data(as_text=True)
    comment = html[html.index('class="comment"'):]
    assert "<strong>bold</strong>" in comment and "<blockquote" in comment
    assert "&lt;script&gt;" in comment and "<script>alert" not in comment
    assert "onerror=alert(2)" not in comment.replace("&lt;img src=x onerror=alert(2)&gt;", "")  # escaped, not an attribute
    assert 'href="javascript:' not in comment and "data:text/html" not in comment.replace("![x](data:text/html;base64,AAAA)", "")
    assert "&lt;b&gt;code&lt;/b&gt;" in comment


def test_comment_attachment_names_resolve_in_that_comment(client):
    import io
    post(client, "/task/TKT-101/comment", content_type="multipart/form-data",
         data={"body": "![pic](pic.png)", "csrf_token": client.token, "files": [(io.BytesIO(b"\x89PNG"), "pic.png")]})
    cid = client.eng.get_comments("TKT-101")[0]["id"]
    assert f'src="/task/TKT-101/attachment/{cid}/pic.png"' in client.get("/task/TKT-101").get_data(as_text=True)


def test_markdown_preview_endpoint(md_client):
    h = {"X-CSRF-Token": md_client.token}
    r = md_client.post("/api/markdown", json={"text": "**x** <i>y</i>"}, headers=h)
    assert r.status_code == 200 and r.get_json()["html"].strip() == '<p dir="auto"><strong>x</strong> &lt;i&gt;y&lt;/i&gt;</p>'
    md_client.eng.save_task({"id": "TKT-101"}, files=[("a.png", b"\x89PNG")])
    r = md_client.post("/api/markdown", json={"text": "![a](a.png)", "task_id": "TKT-101", "owner": "description"}, headers=h)
    assert "/task/TKT-101/attachment/description/a.png" in r.get_json()["html"]
    assert md_client.post("/api/markdown", json={"text": "x"}).status_code == 403            # CSRF
    assert md_client.post("/api/markdown", json={"text": "x", "task_id": "NOPE", "owner": "description"},
                          headers=h).status_code == 200                                      # unknown ticket: no resolving
    assert md_client.post("/api/markdown", json={}, headers=h).get_json()["html"] == ""


def page_without_cheatsheet(page):
    """A page's HTML minus the cheatsheet dialog, which has a diagram example of its own."""
    html = page if isinstance(page, str) else page.get_data(as_text=True)
    start = html.index('<dialog id="md-help"')
    return html[:start] + html[html.index("</dialog>", start):]


def test_mermaid_diagrams_in_markdown(md_client):
    md_client.eng.save_task({"id": "TKT-101", "description": "```mermaid\ngraph LR\n  A --> B\n```"})
    html = md_client.get("/task/TKT-101").get_data(as_text=True)
    assert '<pre class="mermaid" dir="ltr">graph LR\n  A --&gt; B\n</pre>' in html
    # the page tells its script where the bundled library is, and the library is really served
    assert '<meta name="mermaid-src" content="/static/vendor/mermaid.min.js">' in html
    lib = md_client.get("/static/vendor/mermaid.min.js")
    assert lib.status_code == 200 and b'globalThis["mermaid"]' in lib.data[-400:]
    assert b"MIT" in md_client.get("/static/vendor/mermaid.LICENSE").data
    assert 'src="/static/vendor' not in html          # loaded on demand by app.js, not on every page
    assert "cdn" not in html.lower() and "jsdelivr" not in html.lower()               # fully offline
    post(md_client, "/task/TKT-101/comment", data={"body": "```mermaid\nsequenceDiagram\n  A->>B: hi\n```",
                                                    "csrf_token": md_client.token})
    assert page_without_cheatsheet(html).count('class="mermaid"') == 1                  # description only so far
    assert page_without_cheatsheet(md_client.get("/task/TKT-101")).count('class="mermaid"') == 2  # + the comment
    pv = md_client.post("/api/markdown", json={"text": "```mermaid\npie\n \"a\": 1\n```"},
                        headers={"X-CSRF-Token": md_client.token}).get_json()["html"]
    assert 'class="mermaid"' in pv                                                      # the Preview toggle draws it too


def test_cheatsheet_button_and_dialog(md_client):
    html = md_client.get("/task/TKT-101").get_data(as_text=True)
    assert '<dialog id="md-help" class="md-help">' in html and "Markdown cheatsheet" in html
    assert html.count("data-md-help") >= 2          # the description editor and the comment box (+ own comments)
    dialog = html[html.index('<dialog id="md-help"'):html.index("</dialog>", html.index('<dialog id="md-help"'))]
    import markup
    from markupsafe import escape
    for key, source, drawn in markup.CHEATSHEET:
        assert str(escape(source)) in dialog                                       # what you type, escaped
        if drawn:
            assert str(markup.render(source)).strip() in dialog                    # what you get, from the renderer
    assert "<strong>bold</strong>" in dialog and 'class="mermaid"' in dialog and "Not supported" in dialog
    assert "Headings" in dialog and "Tables" in dialog and "You type" in dialog


def test_cheatsheet_is_on_the_new_task_page_and_in_hebrew(client):
    html = client.get("/task/new").get_data(as_text=True)
    assert 'id="md-help"' in html and "data-md-help" in html
    r = post(client, "/lang", data={"lang": "he", "csrf_token": client.token})
    he = client.get("/task/new").get_data(as_text=True)
    assert "דף עזר ל-Markdown" in he and "כותרות" in he and "מה שכותבים" in he and "דף עזר</button>" in he
    assert "Headings" not in he and "Cheatsheet" not in he                    # every label is translated
