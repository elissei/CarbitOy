from decimal import Decimal
from uuid import uuid4

import pytest

from http_auth_support import AuthenticatedTestClient as TestClient

from http_auth_support import create_test_app as create_app
from webapp.auth_csrf import SESSION_COOKIE


def client_for(tmp_path):
    return TestClient(create_app(tmp_path / "test.db"))


def test_inquiry_can_be_saved_without_labour_time(tmp_path):
    with client_for(tmp_path) as client:
        response = client.post(
            "/api/works",
            json={
                "idempotency_key": str(uuid4()),
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
                "idempotency_key": str(uuid4()),
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
                "idempotency_key": str(uuid4()),
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
                "idempotency_key": str(uuid4()),
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
                "idempotency_key": str(uuid4()),
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
                "idempotency_key": str(uuid4()),
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


def inquiry_payload():
    return {
        "idempotency_key": str(uuid4()),
        "registration": "TEST-404",
        "customer_description": "Keksitty regressiotestin tiedustelu",
        "actor": "Testikirjaaja",
    }


def labour_payload(version=1, hours="1.60"):
    return {
        "version": version,
        "hours": hours,
        "work_description": "Keksitty tarkistettu testiaika",
        "recorded_by": "Testikirjaaja",
    }


def test_creation_idempotency_survives_restart_and_preserves_original_response(tmp_path):
    database = tmp_path / "test.db"
    payload = inquiry_payload()
    with TestClient(create_app(database)) as client:
        token = client.cookies.get(SESSION_COOKIE)
        original = client.post("/api/works", json=payload).json()
        client.put(f"/api/works/{original['id']}/labour", json=labour_payload())
    with TestClient(create_app(database)) as client:
        # Retry as the same stable user, rather than the new fixture account.
        client.cookies.set(SESSION_COOKIE, token)
        repeated = client.post("/api/works", json=payload)
        assert repeated.status_code == 201
        assert repeated.json() == original
        current = client.get(f"/api/works/{original['id']}").json()
        assert current["version"] == 2
        assert len(current["events"]) == 2
        assert len(client.get("/api/works").json()) == 1


@pytest.mark.parametrize("changed", [
    {"customer_description": "Eri testisisältö"},
    {"registration": "TEST-OTHER"},
    {"actor": "Toinen testikirjaaja"},
])
def test_same_key_content_conflicts_but_ignored_actor_changes_do_not(tmp_path, changed):
    with client_for(tmp_path) as client:
        payload = inquiry_payload()
        original = client.post("/api/works", json=payload).json()
        response = client.post(
            "/api/works", json={**payload, **changed}
        )
        if "actor" in changed:
            assert response.status_code == 201
            assert response.json() == original
        else:
            assert response.status_code == 409
            assert isinstance(response.json()["detail"], str)
        assert client.get(f"/api/works/{original['id']}").json() == original
        assert len(client.get("/api/works").json()) == 1


def test_identical_intentional_creations_with_distinct_keys_are_allowed(tmp_path):
    with client_for(tmp_path) as client:
        payload = inquiry_payload()
        first = client.post("/api/works", json=payload).json()
        second = client.post(
            "/api/works", json={**payload, "idempotency_key": str(uuid4())}
        ).json()
        assert first["id"] != second["id"]


def test_concurrent_creation_with_same_key_creates_one_work(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    with client_for(tmp_path) as client:
        payload = inquiry_payload()
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(
                lambda _: client.post("/api/works", json=payload), range(2)
            ))
        assert [response.status_code for response in responses] == [201, 201]
        assert responses[0].json() == responses[1].json()
        assert len(client.get("/api/works").json()) == 1


@pytest.mark.parametrize("hours", ["0.00001", "1e100", "0", "-1", "NaN", "Infinity"])
def test_invalid_hours_return_422_without_work_or_event_changes(tmp_path, hours):
    with client_for(tmp_path) as client:
        work = client.post("/api/works", json=inquiry_payload()).json()
        work = client.put(
            f"/api/works/{work['id']}/labour", json=labour_payload()
        ).json()
        response = client.put(
            f"/api/works/{work['id']}/labour",
            json=labour_payload(version=work["version"], hours=hours),
        )
        assert response.status_code == 422
        assert client.get(f"/api/works/{work['id']}").json() == work


def test_concurrent_updates_keep_one_version_and_one_event(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    with client_for(tmp_path) as client:
        work = client.post("/api/works", json=inquiry_payload()).json()
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(
                lambda _: client.put(
                    f"/api/works/{work['id']}/labour", json=labour_payload()
                ), range(2),
            ))
        assert sorted(response.status_code for response in responses) == [200, 409]
        updated = client.get(f"/api/works/{work['id']}").json()
        assert updated["version"] == 2
        assert len(updated["events"]) == 2


def test_failed_creation_rolls_back_work_event_and_idempotency_key(tmp_path, monkeypatch):
    app = create_app(tmp_path / "test.db")
    payload = inquiry_payload()
    original_insert = app.state.repository._insert_event

    def fail_event(*args, **kwargs):
        raise RuntimeError("Event insert failed")

    with TestClient(app) as client:
        monkeypatch.setattr(app.state.repository, "_insert_event", fail_event)
        with pytest.raises(RuntimeError, match="Event insert failed"):
            client.post("/api/works", json=payload)
        assert client.get("/api/works").json() == []
        monkeypatch.setattr(app.state.repository, "_insert_event", original_insert)
        response = client.post("/api/works", json=payload)
        assert response.status_code == 201
        assert len(response.json()["events"]) == 1


def test_unexpected_pricing_error_is_not_hidden(tmp_path, monkeypatch):
    def programming_error(*args, **kwargs):
        raise RuntimeError("Unexpected programming error")

    # 1B-1 moved the snapshot calculation; the propagation contract is unchanged.
    monkeypatch.setattr("webapp.labour_snapshot.calculate_labour_cost", programming_error)
    with client_for(tmp_path) as client:
        work = client.post("/api/works", json=inquiry_payload()).json()
        with pytest.raises(RuntimeError, match="Unexpected programming error"):
            client.put(f"/api/works/{work['id']}/labour", json=labour_payload())
        assert client.get(f"/api/works/{work['id']}").json() == work


def test_new_events_include_resulting_work_version(tmp_path):
    with client_for(tmp_path) as client:
        work = client.post("/api/works", json=inquiry_payload()).json()
        assert work["events"][0]["work_version"] == 1
        updated = client.put(
            f"/api/works/{work['id']}/labour", json=labour_payload()
        ).json()
        assert [event["work_version"] for event in updated["events"]] == [2, 1]


def test_work_and_events_share_a_read_snapshot(tmp_path, monkeypatch):
    import sqlite3
    from workshop_support import ScopedTestRepository as WorkRepository

    database = tmp_path / "test.db"
    repository = WorkRepository(database)
    repository.initialize()
    # WAL permits another connection to commit while this reader holds its snapshot.
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
    work = repository.create_work(
        registration="TEST-READ", customer_description="Keksitty lukutesti",
        actor="Testikirjaaja", idempotency_key=str(uuid4()),
    )
    original_connect = sqlite3.connect
    triggered = False
    labour = {
        "hours": "1.60", "source": "MANUAL_AUTODATA", "work_description": "Testityö",
        "recorded_by": "Testikirjaaja", "autodata_reference": "",
        "hourly_rate": "90.00", "total_price": "144.00", "net_price": "114.74",
        "vat_price": "29.26", "vat_rate": "0.255", "currency": "EUR",
    }

    class InterleavedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            nonlocal triggered
            if "SELECT occurred_at" in sql and not triggered:
                triggered = True
                repository.record_labour(
                    work_id=work["id"], expected_version=1, labour=labour
                )
            return super().execute(sql, parameters)

    monkeypatch.setattr(
        "webapp.database.sqlite3.connect",
        lambda *args, **kwargs: original_connect(
            *args, **kwargs, factory=InterleavedConnection
        ),
    )
    assert repository.get_work(work["id"]) == work
    assert triggered
    current = repository.get_work(work["id"])
    assert current["version"] == current["events"][0]["work_version"] == 2


def test_old_schema_is_migrated_without_changing_existing_data(tmp_path):
    import sqlite3

    database = tmp_path / "legacy.db"
    # Build the exact legacy schema, independently of the new SCHEMA constant.
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE work_inquiries (
                id TEXT PRIMARY KEY, registration TEXT NOT NULL,
                customer_description TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('KESKEN')),
                version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                labour_hours TEXT, labour_source TEXT, labour_work_description TEXT,
                labour_recorded_by TEXT, autodata_reference TEXT, hourly_rate TEXT,
                total_price TEXT, net_price TEXT, vat_price TEXT, vat_rate TEXT, currency TEXT
            );
            CREATE TABLE work_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                work_id TEXT NOT NULL REFERENCES work_inquiries(id), occurred_at TEXT NOT NULL,
                event_type TEXT NOT NULL, actor TEXT NOT NULL, details_json TEXT NOT NULL
            );
            INSERT INTO work_inquiries
                (id, registration, customer_description, status, version, created_at, updated_at)
                VALUES ('legacy', 'TEST-OLD', 'Keksitty vanha tiedustelu', 'KESKEN', 1,
                        '2026-10-01T10:00:00+00:00', '2026-10-01T10:00:00+00:00');
            INSERT INTO work_events (id, work_id, occurred_at, event_type, actor, details_json)
                VALUES (7, 'legacy', '2026-10-01T10:00:00+00:00', 'TIEDUSTELU_LUOTU',
                        'Testikirjaaja', '{"registration":"TEST-OLD"}');
        """)
        before_work = connection.execute("SELECT * FROM work_inquiries").fetchone()
        before_event = connection.execute("SELECT * FROM work_events").fetchone()
    for _ in range(2):
        with TestClient(create_app(database)) as client:
            old = client.get("/api/works/legacy").json()
            assert old["events"][0]["work_version"] is None
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM work_inquiries").fetchone() == before_work
        assert connection.execute("SELECT * FROM work_events").fetchone() == (*before_event, None)
    with TestClient(create_app(database)) as client:
        updated = client.put("/api/works/legacy/labour", json=labour_payload()).json()
        assert [event["work_version"] for event in updated["events"]] == [2, None]
