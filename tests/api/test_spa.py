import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app


@pytest.fixture
def web(tmp_path, make_settings):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    (tmp_path / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    with TestClient(create_app(make_settings(web_dist=tmp_path))) as client:
        yield client


def test_index_and_assets_are_served(web):
    assert web.get("/").text == "<html>spa</html>"
    assert web.get("/assets/app.js").text == "console.log(1)"


def test_client_routes_fall_back_to_index(web):
    assert web.get("/events/1").text == "<html>spa</html>"


def test_unknown_api_path_is_json_error_not_index(web):
    response = web.get("/api/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_api_still_works_beside_spa(web):
    assert web.get("/api/health").json()["status"] == "ok"


def test_no_web_dist_means_no_spa(make_settings, tmp_path):
    with TestClient(create_app(make_settings(web_dist=tmp_path / "missing"))) as client:
        assert client.get("/").status_code == 404
