from fastapi.testclient import TestClient

from app.web import app


def test_health_endpoint():
    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "carbit-demo",
    }


def test_homepage_serves_finnish_html():
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<html lang="fi">' in response.text
    assert "Uusi työtiedustelu" in response.text
    assert "Ihmisen varaosavalinta" in response.text
    assert 'id="message"' in response.text
