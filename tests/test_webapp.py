from decimal import Decimal

from fastapi.testclient import TestClient

from webapp.main import create_app


def client_for(tmp_path):
    return TestClient(create_app(tmp_path / "test.db"))


def test_inquiry_can_be_saved_without_labour_time(tmp_path):
    with client_for(tmp_path) as client:
        response = client.post(
            "/api/works",
            json={
                "registration": "abc-123",
                "customer_description": "Jarruista kuuluu ääntä.",
                "actor": "Testikäyttäjä",
            },
        )

        assert response.status_code == 201
        work = response.json()
        assert work["registration"] == "ABC-123"
        assert work["status"] == "KESKEN"
        assert work["labour_hours"] is None
        assert work["version"] == 1
        assert work["events"][0]["event_type"] == "TIEDUSTELU_LUOTU"

        listed = client.get("/api/works").json()
        assert [item["id"] for item in listed] == [work["id"]]


def test_manual_autodata_labour_is_priced_with_domain_service(tmp_path):
    with client_for(tmp_path) as client:
        work = client.post(
            "/api/works",
            json={
                "registration": "xyz-789",
                "customer_description": "Keksitty testityö.",
                "actor": "Vastaanotto",
            },
        ).json()

        response = client.put(
            f"/api/works/{work['id']}/labour",
            json={
                "version": work["version"],
                "hours": "1.60",
                "work_description": "Keksitty Autodata-työkuvaus",
                "recorded_by": "Mekaanikko",
                "autodata_reference": "TEST-REF-1",
            },
        )

        assert response.status_code == 200
        saved = response.json()
        assert Decimal(saved["total_price"]) == Decimal("144.00")
        assert Decimal(saved["net_price"]) == Decimal("114.74")
        assert Decimal(saved["vat_price"]) == Decimal("29.26")
        assert saved["labour_source"] == "MANUAL_AUTODATA"
        assert saved["hourly_rate"] == "90.00"
        assert saved["vat_rate"] == "0.255"
        assert saved["version"] == 2
        assert saved["events"][0]["event_type"] == "AUTODATA_TYOAIKA_KIRJATTU"


def test_stale_browser_version_cannot_overwrite_newer_change(tmp_path):
    with client_for(tmp_path) as client:
        work = client.post(
            "/api/works",
            json={
                "registration": "QWE-456",
                "customer_description": "Keksitty testityö.",
                "actor": "Vastaanotto",
            },
        ).json()
        payload = {
            "version": 1,
            "hours": "2.00",
            "work_description": "Ensimmäinen kirjaus",
            "recorded_by": "Mekaanikko",
            "autodata_reference": None,
        }
        assert client.put(f"/api/works/{work['id']}/labour", json=payload).status_code == 200

        stale = client.put(f"/api/works/{work['id']}/labour", json=payload)

        assert stale.status_code == 409
        current = client.get(f"/api/works/{work['id']}").json()
        assert current["version"] == 2
        assert len(current["events"]) == 2


def test_work_change_and_event_are_committed_together(tmp_path, monkeypatch):
    app = create_app(tmp_path / "test.db")
    with TestClient(app) as client:
        work = client.post(
            "/api/works",
            json={
                "registration": "RTY-321",
                "customer_description": "Keksitty testityö.",
                "actor": "Vastaanotto",
            },
        ).json()

        def fail_event(*args, **kwargs):
            raise RuntimeError("event insert failed")

        monkeypatch.setattr(app.state.repository, "_insert_event", fail_event)
        try:
            client.put(
                f"/api/works/{work['id']}/labour",
                json={
                    "version": 1,
                    "hours": "1.00",
                    "work_description": "Kirjaus epäonnistuu",
                    "recorded_by": "Mekaanikko",
                },
            )
        except RuntimeError:
            pass

        saved = app.state.repository.get_work(work["id"])
        assert saved["version"] == 1
        assert saved["labour_hours"] is None
        assert len(saved["events"]) == 1


def test_inquiry_survives_application_restart(tmp_path):
    database_path = tmp_path / "persistent.db"
    with TestClient(create_app(database_path)) as client:
        work = client.post(
            "/api/works",
            json={
                "registration": "TEST-202",
                "customer_description": "Keksitty pysyvyyden testitiedustelu",
                "actor": "Testikäyttäjä",
            },
        ).json()

    with TestClient(create_app(database_path)) as client:
        restored = client.get(f"/api/works/{work['id']}").json()
        assert restored == work


def test_history_preserves_previous_pricing_snapshot(tmp_path):
    with client_for(tmp_path) as client:
        work = client.post(
            "/api/works",
            json={
                "registration": "TEST-303",
                "customer_description": "Keksitty historian testitiedustelu",
                "actor": "Testikäyttäjä",
            },
        ).json()
        for version, hours in [(1, "1.60"), (2, "2.55")]:
            response = client.put(
                f"/api/works/{work['id']}/labour",
                json={
                    "version": version,
                    "hours": hours,
                    "work_description": "Keksitty tarkistettu testiaika",
                    "recorded_by": "Testikirjaaja",
                },
            )
            assert response.status_code == 200
        latest = response.json()
        assert latest["total_price"] == "229.50"
        previous = latest["events"][1]["details"]
        assert previous["hours"] == "1.60"
        assert previous["hourly_rate"] == "90.00"
        assert previous["total_price"] == "144.00"
        assert previous["vat_rate"] == "0.255"
        assert previous["source"] == "MANUAL_AUTODATA"
