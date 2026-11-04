"""SPA hosting checks, independent of PostgreSQL and the analysis worker."""

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from app.frontend import SPAStaticFiles


@pytest.fixture(scope="session", autouse=True)
def database():
    yield


@pytest.fixture(autouse=True)
def clean_training():
    yield


@pytest.fixture
def frontend_client(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>Tactics SPA</title>", encoding="utf-8")
    (dist / "assets").mkdir()
    (dist / "assets" / "app.js").write_text("console.log('ready')", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside-build-secret", encoding="utf-8")
    (dist / "escaped.txt").symlink_to(tmp_path / "secret.txt")
    app = FastAPI()

    @app.get("/api/example")
    def example():
        return {"example": True}

    @app.api_route("/api/{unmatched:path}", methods=["GET", "HEAD"])
    def api_not_found(unmatched: str):
        raise HTTPException(404, "Unknown API endpoint")

    app.mount("/", SPAStaticFiles(directory=dist, html=True))
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("path", [
    "/", "/training/12", "/progress", "/games/7", "/analysis/5/review",
    "/coach/skills?skill=fork", "/coach/practice/3", "/login?next=%2Fprogress", "/unknown-route",
])
def test_deep_links_serve_application(frontend_client, path):
    response = frontend_client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Tactics SPA" in response.text


def test_head_deep_link_has_same_headers_without_body(frontend_client):
    get = frontend_client.get("/coach/practice/3")
    head = frontend_client.head("/coach/practice/3")
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-type"] == get.headers["content-type"]
    assert head.headers["content-length"] == get.headers["content-length"]


@pytest.mark.parametrize("path", ["/assets/missing.js", "/assets/missing", "/pieces/missing.svg", "/favicon.svg", "/.env"])
def test_missing_assets_do_not_return_application(frontend_client, path):
    response = frontend_client.get(path)
    assert response.status_code == 404
    assert "Tactics SPA" not in response.text


def test_existing_assets_and_api_keep_their_responses(frontend_client):
    asset = frontend_client.get("/assets/app.js")
    assert asset.status_code == 200 and asset.text == "console.log('ready')"
    assert frontend_client.get("/api/example").json() == {"example": True}
    unknown = frontend_client.get("/api/missing")
    assert unknown.status_code == 404 and unknown.json() == {"detail": "Unknown API endpoint"}
    assert frontend_client.get("/api").status_code == 404


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_non_read_methods_are_not_rewritten(frontend_client, method):
    response = frontend_client.request(method, "/coach/practice/3")
    assert response.status_code == 405
    assert "Tactics SPA" not in response.text


@pytest.mark.parametrize("path", ["/%2e%2e/secret.txt", "/assets/%2e%2e/%2e%2e/secret.txt", "/escaped.txt"])
def test_traversal_and_symlinks_cannot_escape_build(frontend_client, path):
    response = frontend_client.get(path)
    assert response.status_code == 404
    assert "outside-build-secret" not in response.text
