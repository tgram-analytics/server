"""Root marketing redirect keeps /mcp and health untouched."""

from fastapi.testclient import TestClient

from app.main import create_app


def test_root_permanently_redirects_to_marketing_site() -> None:
    client = TestClient(create_app())
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == "https://tgram-analytics.com/"


def test_health_still_ok() -> None:
    client = TestClient(create_app())
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
