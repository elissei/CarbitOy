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


def test_intake_ready_returns_candidates_and_preserves_finnish_text():
    message = "ABC-123, etujarrupalat pitäisi vaihtaa"

    with TestClient(app) as client:
        response = client.post(
            "/api/intake",
            json={"message": message, "mode": "replay"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["message"] == message
    assert data["mode"] == "replay"
    assert data["interpretation"]["status"] == "READY"
    assert data["human_required_reason"] is None
    assert len(data["proposal"]["candidates"]) == 2
    assert "final_quote" not in data


def test_intake_diagnosis_returns_no_proposal():
    with TestClient(app) as client:
        response = client.post(
            "/api/intake",
            json={
                "message": "ABC-123, autosta kuuluu outoa ääntä edestä",
                "mode": "replay",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["interpretation"]["status"] == "HUMAN_REQUIRED"
    assert data["interpretation"]["diagnosis_required"] is True
    assert data["human_required_reason"] == "Diagnoosin tarve"
    assert data["proposal"] is None


def test_intake_replay_rejects_unknown_message():
    with TestClient(app) as client:
        response = client.post(
            "/api/intake",
            json={"message": "Vaihda moottori", "mode": "replay"},
        )

    assert response.status_code == 422


def test_intake_rejects_blank_message():
    with TestClient(app) as client:
        response = client.post(
            "/api/intake",
            json={"message": "   ", "mode": "replay"},
        )

    assert response.status_code == 422


def test_intake_openai_without_key_returns_clear_error(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with TestClient(app) as client:
        response = client.post(
            "/api/intake",
            json={
                "message": "ABC-123, etujarrupalat pitäisi vaihtaa",
                "mode": "openai",
            },
        )

    assert response.status_code == 503
    assert "OpenAI-avain puuttuu" in response.json()["detail"]
