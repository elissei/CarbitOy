"""Golden regression for the v1 manual labour route, written before 1B-1.

It pins the response, the price snapshot and the event of
PUT /api/works/{id}/labour exactly, so the shared snapshot builder and
_apply_labour refactor can be shown to keep the v1 behaviour unchanged.
"""

from uuid import uuid4

import pytest

from http_auth_support import AuthenticatedTestClient as TestClient
from http_auth_support import create_test_app as create_app


WORK_KEYS = {
    "id", "registration", "customer_description", "status", "version", "created_at",
    "updated_at", "labour_hours", "labour_source", "labour_work_description",
    "labour_recorded_by", "autodata_reference", "hourly_rate", "total_price", "net_price",
    "vat_price", "vat_rate", "currency", "events",
}
SNAPSHOT_KEYS = {
    "hours", "source", "work_description", "recorded_by", "autodata_reference",
    "hourly_rate", "total_price", "net_price", "vat_price", "vat_rate", "currency",
}


def create_work(client):
    return client.post("/api/works", json={
        "idempotency_key": str(uuid4()), "registration": "gold-101",
        "customer_description": "Keksitty golden-testin tiedustelu",
    }).json()


@pytest.mark.parametrize(("hours", "reference", "stored_reference", "total", "net", "vat"), [
    ("1.60", "  GOLD-REF-1  ", "GOLD-REF-1", "144.00", "114.74", "29.26"),
    ("0.1235", None, "", "11.12", "8.86", "2.26"),
    ("2.55", "   ", "", "229.50", "182.87", "46.63"),
    ("0.125", "GOLD-REF-2", "GOLD-REF-2", "11.25", "8.96", "2.29"),
])
def test_manual_route_response_snapshot_and_event_are_unchanged(
    tmp_path, hours, reference, stored_reference, total, net, vat,
):
    with TestClient(create_app(tmp_path / "golden.db")) as client:
        work = create_work(client)
        user_id = str(client.grant.user.user_id)
        body = {"version": 1, "hours": hours, "work_description": "  Keksitty golden-työ  "}
        if reference is not None:
            body["autodata_reference"] = reference
        response = client.put(f"/api/works/{work['id']}/labour", json=body)

        assert response.status_code == 200
        saved = response.json()
        assert set(saved) == WORK_KEYS
        expected_work = {
            "id": work["id"], "registration": "GOLD-101",
            "customer_description": "Keksitty golden-testin tiedustelu", "status": "KESKEN",
            "version": 2, "created_at": work["created_at"],
            "labour_hours": hours, "labour_source": "MANUAL_AUTODATA",
            "labour_work_description": "Keksitty golden-työ", "labour_recorded_by": user_id,
            "autodata_reference": stored_reference, "hourly_rate": "90.00",
            "total_price": total, "net_price": net, "vat_price": vat, "vat_rate": "0.255",
            "currency": "EUR",
        }
        assert {key: saved[key] for key in expected_work} == expected_work
        assert saved["updated_at"] >= work["updated_at"]

        assert [event["event_type"] for event in saved["events"]] == [
            "AUTODATA_TYOAIKA_KIRJATTU", "TIEDUSTELU_LUOTU",
        ]
        event = saved["events"][0]
        assert event["actor"] == user_id
        assert event["work_version"] == 2
        assert event["occurred_at"] == saved["updated_at"]
        details = dict(event["details"])
        attribution = details.pop("actor_attribution")
        assert set(details) == SNAPSHOT_KEYS
        assert details == {
            "hours": hours, "source": "MANUAL_AUTODATA", "work_description": "Keksitty golden-työ",
            "recorded_by": user_id, "autodata_reference": stored_reference,
            "hourly_rate": "90.00", "total_price": total, "net_price": net, "vat_price": vat,
            "vat_rate": "0.255", "currency": "EUR",
        }
        assert attribution["user_id"] == user_id
        assert attribution["provenance"] == "authenticated_session"
        assert client.get(f"/api/works/{work['id']}").json() == saved


@pytest.mark.parametrize("hours", ["0.00001", "1e100"])
def test_unpriceable_hours_keep_the_422_detail_and_change_nothing(tmp_path, hours):
    with TestClient(create_app(tmp_path / "golden.db")) as client:
        work = create_work(client)
        response = client.put(f"/api/works/{work['id']}/labour", json={
            "version": 1, "hours": hours, "work_description": "Keksitty golden-työ",
        })
        assert response.status_code == 422
        assert response.json() == {"detail": (
            "Työaika: annettu aika ei tuota laskettavissa olevaa "
            "positiivista senttihintaa. Tarkista Autodatasta kirjattu aika."
        )}
        assert client.get(f"/api/works/{work['id']}").json() == work


def test_stale_version_keeps_the_409_detail_and_change_nothing(tmp_path):
    with TestClient(create_app(tmp_path / "golden.db")) as client:
        work = create_work(client)
        body = {"version": 1, "hours": "1.60", "work_description": "Keksitty golden-työ"}
        updated = client.put(f"/api/works/{work['id']}/labour", json=body).json()
        stale = client.put(f"/api/works/{work['id']}/labour", json=body)
        assert stale.status_code == 409
        assert stale.json() == {"detail": (
            "Työtä on muutettu toisessa selainikkunassa. "
            "Lataa uusin versio ennen tallennusta."
        )}
        assert client.get(f"/api/works/{work['id']}").json() == updated
