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
    r = post(client, "/task/TKT-101", data={"title": "x", "status": "todo", "blocked_by": "TKT-102"})
    assert r.status_code == 400
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
    # TODO(i18n): Hebrew strings
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
