import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.web import mount_workbench


def test_only_built_assets_are_served_with_identity_bound_csp(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "assets").mkdir()
    (dist / "index.html").write_text("<html>synthetic</html>")
    (dist / "assets" / "app.js").write_text("/* synthetic */")
    (tmp_path / "private.json").write_text("PRIVATE")
    app = FastAPI()
    mount_workbench(app, dist, "https://identity.example.invalid/realms/test")
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "https://identity.example.invalid" in response.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert client.get("/assets/app.js").status_code == 200
        assert client.get("/assets/../private.json").status_code == 404
        assert client.get("/private.json").status_code == 404
    with pytest.raises(ValueError):
        mount_workbench(FastAPI(), tmp_path / "missing", "https://identity.example.invalid")
