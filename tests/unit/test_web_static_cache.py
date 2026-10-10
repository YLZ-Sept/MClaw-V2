"""The web shell must never be served from cache.

A stale ``index.html`` keeps pointing at the previous bundle, and a stale
``sw.js`` keeps a service worker serving the previous build outright -- so a
redeploy would appear to do nothing until the user force-refreshes.  Hashed
assets must keep caching normally, since their filename changes with content.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mclaw.api.server import WebStaticFiles


def _client(tmp_path) -> TestClient:
    (tmp_path / "index.html").write_text("<html>shell</html>", encoding="utf-8")
    (tmp_path / "sw.js").write_text("// service worker", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app-abc123.js").write_text("// bundle", encoding="utf-8")

    app = FastAPI()
    app.mount("/web", WebStaticFiles(directory=str(tmp_path), html=True), name="web")
    return TestClient(app)


def test_shell_documents_are_not_cached(tmp_path):
    client = _client(tmp_path)
    for path in ("/web/", "/web/index.html", "/web/sw.js"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers.get("cache-control") == "no-store", path


def test_hashed_assets_keep_normal_caching(tmp_path):
    response = _client(tmp_path).get("/web/assets/app-abc123.js")
    assert response.status_code == 200
    assert "no-store" not in response.headers.get("cache-control", "")


def test_directory_request_serves_shell_without_caching(tmp_path):
    # ``html=True`` serves index.html for a *directory* request.  The SPA uses
    # hash routing (#/view), so a document is only ever fetched from /web/ or
    # /web/index.html -- a path-based deep link is never issued.
    response = _client(tmp_path).get("/web/")
    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-store"


def test_unknown_non_directory_path_404s(tmp_path):
    # Documents the real behaviour: ``html=True`` does *not* rewrite arbitrary
    # unknown paths to index.html (upstream special-cases its single return URL
    # for the same reason).  Harmless here because routing is hash-based -- this
    # exists so nobody "fixes" the 404 into a silent catch-all.
    assert _client(tmp_path).get("/web/some/spa/route").status_code == 404


def test_conditional_request_does_not_yield_304_for_shell(tmp_path):
    # A stale 304 would let the browser keep the cached shell; the conditional
    # headers must be dropped so the shell is always re-sent.
    client = _client(tmp_path)
    response = client.get(
        "/web/index.html",
        headers={"If-None-Match": '"stale-etag"', "If-Modified-Since": "Wed, 21 Oct 2015 07:28:00 GMT"},
    )
    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-store"
