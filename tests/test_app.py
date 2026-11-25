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
    assert 'href="https://example.com"' in html and "<br>" in html
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
    assert html.count("data-open-attachment") == 2  # the description's file and the comment's
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
