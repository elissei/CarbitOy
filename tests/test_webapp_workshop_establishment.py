"""New-environment establishment: one organization and one ACTIVE workshop, offline only."""

import json
import os
import sqlite3
import sys
import threading
from uuid import NAMESPACE_OID, uuid4, uuid5

import pytest
from cryptography.fernet import Fernet

import webapp.workshop_admin as admin
from webapp.auth_crypto import TotpCipher
from webapp.auth_models import UserRole
from webapp.auth_repository import AuthRepository
from webapp.auth_service import AuthService
from webapp.database import WorkRepository
from webapp.workshop_admin import (
    CARBIT_REPAIR_ORGANIZATION, INITIAL_WORKSHOP_OPERATION, WORKSHOP_1, WORKSHOP_2,
    WorkshopEstablishmentError, establish_initial_workshop,
)
from webapp.workshop_context import TrustedWorkshopContext, WorkshopAccessDenied, permissions_for_role
from webapp.workshop_repository import WorkshopDirectory, audit


ORGANIZATION = str(uuid5(NAMESPACE_OID, "carbit.tests.establishment.organization"))
WORKSHOP = str(uuid5(NAMESPACE_OID, "carbit.tests.establishment.workshop"))
PASSWORD = "Fictional establishment regression passphrase"


def request(**overrides):
    values = {"organization_id": ORGANIZATION, "organization_name": "Fictional repair organization",
              "workshop_id": WORKSHOP, "workshop_name": "Fictional workshop",
              "operator_reference": "Explicit fictional establishment approval"}
    return values | overrides


def expected_details(**overrides):
    values = {"organization_id": ORGANIZATION, "organization_name": "Fictional repair organization",
              "workshop_id": WORKSHOP, "workshop_name": "Fictional workshop", "workshop_status": "ACTIVE"}
    return values | overrides


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "works.db"
    WorkRepository(path).initialize()
    return path


def snapshot(path):
    with sqlite3.connect(path) as connection:
        objects = connection.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        tables = [row[1] for row in objects if row[0] == "table" and not row[1].startswith("sqlite_")]
        return {"schema": objects, **{table: connection.execute(
            f'SELECT * FROM "{table}" ORDER BY rowid').fetchall() for table in tables}}


def rows(path, query, parameters=()):
    with sqlite3.connect(path) as connection:
        return connection.execute(query, parameters).fetchall()


def raw_change(path, *statements):
    """Test-only fixture state for negative cases; never an operator procedure."""
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for statement, parameters in statements:
            connection.execute(statement, parameters)


def auth_directory(tmp_path, database):
    auth = AuthRepository(tmp_path / "auth.db")
    auth.initialize()
    service = AuthService(auth, TotpCipher((Fernet.generate_key(),)))
    return service, WorkshopDirectory(WorkRepository(database), auth)


def run_cli(monkeypatch, *arguments):
    monkeypatch.setattr(sys, "argv", ["workshop_admin", *arguments])
    admin.main()


def cli_arguments(path, **overrides):
    values = request(**overrides)
    return ["establish-initial-workshop", "--database", str(path),
            "--organization-id", values["organization_id"], "--organization-name", values["organization_name"],
            "--workshop-id", values["workshop_id"], "--workshop-name", values["workshop_name"],
            "--operator-reference", values["operator_reference"]]


def test_first_establishment_writes_one_organization_active_workshop_and_audit_event(tmp_path, database):
    result = establish_initial_workshop(database, **request(
        organization_id="{" + ORGANIZATION.upper() + "}", organization_name="  Fictional repair organization ",
        workshop_id=WORKSHOP.upper(), workshop_name=" Fictional workshop  "))
    assert result["status"] == "ESTABLISHED"
    assert (result["organization_id"], result["workshop_id"]) == (ORGANIZATION, WORKSHOP)
    assert rows(database, "SELECT organization_id,name,enabled FROM repair_organizations") == [
        (ORGANIZATION, "Fictional repair organization", 1)]
    assert rows(database, "SELECT workshop_id,organization_id,name,status FROM workshops") == [
        (WORKSHOP, ORGANIZATION, "Fictional workshop", "ACTIVE")]
    events = rows(database, "SELECT event_id,operation,operator_reference,details_json FROM workshop_audit_events")
    assert len(events) == 1 and events[0][:3] == (
        result["audit_event_id"], INITIAL_WORKSHOP_OPERATION, "Explicit fictional establishment approval")
    assert json.loads(events[0][3]) == expected_details()
    for table in ("workshop_principals", "workshop_memberships", "active_user_workshops",
                  "inquiry_workshop_assignments", "work_inquiries"):
        assert rows(database, f"SELECT count(*) FROM {table}") == [(0,)]
    assert rows(database, "PRAGMA foreign_key_check") == []
    assert sorted(item.name for item in tmp_path.iterdir()) == ["works.db"]  # no auth database opened/created
    with pytest.raises(sqlite3.IntegrityError, match="immutable workshop metadata"):
        raw_change(database, ("UPDATE workshop_audit_events SET operator_reference='changed'", ()))


def test_identical_retry_returns_original_event_without_writes(database):
    first = establish_initial_workshop(database, **request())
    before, raw = snapshot(database), database.read_bytes()
    again = establish_initial_workshop(database, **request(operator_reference="Different valid retry reference"))
    assert again == first | {"status": "ALREADY_ESTABLISHED"}
    assert snapshot(database) == before and database.read_bytes() == raw
    assert rows(database, "SELECT operator_reference FROM workshop_audit_events") == [
        ("Explicit fictional establishment approval",)]


def test_retry_after_real_membership_and_work_is_recognised_without_changes(tmp_path, database):
    first = establish_initial_workshop(database, **request())
    service, directory = auth_directory(tmp_path, database)
    user = service.provision_identity(username="establishment.owner", display_name="Fictional owner",
                                      role=UserRole.OWNER, password=PASSWORD)
    directory.grant_membership(user.user_id, WORKSHOP, operator_reference="Explicit fictional membership")
    membership = directory.resolve(user.user_id)
    context = TrustedWorkshopContext(str(user.user_id), permissions_for_role(UserRole.OWNER), **membership)
    WorkRepository(database).create_work(registration="TEST-1", customer_description="Fictional work",
                                         actor="Fictional offline regression", idempotency_key=str(uuid4()),
                                         context=context)
    assert rows(database, "SELECT count(*) FROM inquiry_workshop_assignments") == [(1,)]
    before = snapshot(database)
    again = establish_initial_workshop(database, **request())
    assert again == first | {"status": "ALREADY_ESTABLISHED"}
    assert snapshot(database) == before
    assert rows(database, "SELECT version FROM workshop_memberships") == [(1,)]


def test_matching_retry_rejects_a_database_with_foreign_key_violations_without_changes(database):
    first = establish_initial_workshop(database, **request())
    # Negative-case preparation only: an orphan row written with FK enforcement off.
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("INSERT INTO work_events (work_id,occurred_at,event_type,actor,details_json,work_version) "
                           "VALUES ('missing-work','2026-10-17T00:00:00+00:00','ORPHAN','Fixture','{}',1)")
    assert rows(database, "PRAGMA foreign_key_check")
    before, raw = snapshot(database), database.read_bytes()
    with pytest.raises(WorkshopEstablishmentError, match="foreign-key check failed"):
        establish_initial_workshop(database, **request())
    assert snapshot(database) == before and database.read_bytes() == raw
    assert rows(database, "SELECT event_id FROM workshop_audit_events") == [(first["audit_event_id"],)]


@pytest.mark.parametrize("change", [
    {"organization_id": str(uuid4())}, {"workshop_id": str(uuid4())},
    {"organization_name": "Other organization"}, {"workshop_name": "Other workshop"},
])
def test_conflicting_request_after_establishment_changes_nothing(database, change):
    establish_initial_workshop(database, **request())
    before = snapshot(database)
    with pytest.raises(WorkshopEstablishmentError, match="conflicts"):
        establish_initial_workshop(database, **request(**change))
    assert snapshot(database) == before


ORG_ROW = ("INSERT INTO repair_organizations VALUES (?,?,1)", (ORGANIZATION, "Fictional repair organization"))
WORKSHOP_ROW = ("INSERT INTO workshops VALUES (?,?,?,'ACTIVE')", (WORKSHOP, ORGANIZATION, "Fictional workshop"))


def audited_rows(**details):
    return (ORG_ROW, WORKSHOP_ROW, (
        "INSERT INTO workshop_audit_events (occurred_at,operation,operator_reference,details_json) VALUES (?,?,?,?)",
        ("2026-10-17T00:00:00+00:00", INITIAL_WORKSHOP_OPERATION, "Fixture", json.dumps(expected_details(**details)))))


@pytest.mark.parametrize("state", [
    "organization-only", "rows-without-event", "event-only", "event-with-other-details", "workshop-inactive",
    "workshop-planned", "organization-disabled", "extra-workshop", "second-establishment-event",
])
def test_partial_or_inconsistent_establishment_state_is_rejected_without_repair(database, state):
    statements = {
        "organization-only": (ORG_ROW,),
        "rows-without-event": (ORG_ROW, WORKSHOP_ROW),
        "event-only": audited_rows()[2:],
        "event-with-other-details": audited_rows(workshop_name="Other workshop"),
    }.get(state)
    if statements is None:
        establish_initial_workshop(database, **request())
        statements = {
            "workshop-inactive": (("UPDATE workshops SET status='INACTIVE'", ()),),
            "workshop-planned": (("UPDATE workshops SET status='PLANNED'", ()),),
            "organization-disabled": (("UPDATE repair_organizations SET enabled=0", ()),),
            "extra-workshop": (("INSERT INTO workshops VALUES (?,?,'Second','ACTIVE')", (str(uuid4()), ORGANIZATION)),),
            "second-establishment-event": audited_rows()[2:],
        }[state]
    raw_change(database, *statements)
    before = snapshot(database)
    with pytest.raises(WorkshopEstablishmentError, match="conflicts"):
        establish_initial_workshop(database, **request())
    assert snapshot(database) == before


@pytest.mark.parametrize("data", ["legacy-work", "other-audit-event"])
def test_existing_data_without_an_audited_establishment_is_rejected(database, data):
    raw_change(database, {
        "legacy-work": ("INSERT INTO work_inquiries (id,registration,customer_description,status,version,"
                        "created_at,updated_at) VALUES ('legacy-1','TEST','Fictional legacy','KESKEN',1,"
                        "'2026-10-01T00:00:00+00:00','2026-10-01T00:00:00+00:00')", ()),
        "other-audit-event": ("INSERT INTO workshop_audit_events (occurred_at,operation,operator_reference,"
                              "details_json) VALUES ('2026-10-17T00:00:00+00:00','OTHER','Fixture','{}')", ()),
    }[data])
    before = snapshot(database)
    with pytest.raises(WorkshopEstablishmentError, match="empty new environment"):
        establish_initial_workshop(database, **request())
    assert snapshot(database) == before


@pytest.mark.parametrize("schema", [
    "empty-file", "missing-ledger", "unknown-workshop-version", "incomplete-draft-ledger",
    "old-webapp-ledger", "missing-table", "missing-audit-trigger",
])
def test_unsupported_or_uninitialized_schema_is_rejected_without_migration(tmp_path, database, schema):
    if schema == "empty-file":
        database = tmp_path / "empty.db"
        database.write_bytes(b"")
    else:
        raw_change(database, {
            "missing-ledger": ("DROP TABLE draft_schema_migrations", ()),
            "unknown-workshop-version": ("INSERT INTO workshop_schema_migrations VALUES (2,'2026-10-17')", ()),
            "incomplete-draft-ledger": ("DELETE FROM draft_schema_migrations WHERE version=4", ()),
            "old-webapp-ledger": ("DELETE FROM webapp_schema_migrations WHERE version=2", ()),
            "missing-table": ("DROP TABLE active_user_workshops", ()),
            "missing-audit-trigger": ("DROP TRIGGER immutable_workshop_audit_events_delete", ()),
        }[schema])
    before, raw = snapshot(database), database.read_bytes()
    with pytest.raises(WorkshopEstablishmentError, match="supported schema"):
        establish_initial_workshop(database, **request())
    assert snapshot(database) == before and database.read_bytes() == raw


@pytest.mark.parametrize("change", [
    {"organization_id": "not-a-uuid"}, {"workshop_id": ""}, {"workshop_id": None},
    {"workshop_id": ORGANIZATION}, {"organization_id": CARBIT_REPAIR_ORGANIZATION},
    {"workshop_id": WORKSHOP_1}, {"workshop_id": WORKSHOP_2},
    {"organization_name": "   "}, {"workshop_name": "x" * 201}, {"workshop_name": 7},
    {"operator_reference": "   "}, {"operator_reference": "x" * 501}, {"operator_reference": None},
])
def test_invalid_inputs_are_rejected_before_the_database_is_opened(database, monkeypatch, change):
    def never_open(path):
        raise AssertionError("database opened before input validation")
    monkeypatch.setattr(admin, "_existing_business_connection", never_open)
    with pytest.raises(WorkshopEstablishmentError):
        establish_initial_workshop(database, **request(**change))


def test_missing_relative_and_approved_targets_are_rejected_and_never_created(tmp_path, database, monkeypatch):
    missing = tmp_path / "missing.db"
    with pytest.raises(WorkshopEstablishmentError, match="existing business database"):
        establish_initial_workshop(missing, **request())
    with pytest.raises(WorkshopEstablishmentError, match="absolute"):
        establish_initial_workshop("relative.db", **request())
    # Even if the file vanishes after validation, opening must not create it.
    with pytest.raises(sqlite3.OperationalError):
        with admin._existing_business_connection(missing):
            pass
    assert not missing.exists() and not os.path.exists("relative.db")
    before = snapshot(database)
    monkeypatch.setattr(admin, "APPROVED_DATABASE", database)
    with pytest.raises(WorkshopEstablishmentError, match="approved development database"):
        establish_initial_workshop(database, **request())
    hard_link = tmp_path / "same-file.db"
    os.link(database, hard_link)
    with pytest.raises(WorkshopEstablishmentError, match="approved development database"):
        establish_initial_workshop(hard_link, **request())
    assert snapshot(database) == before


def test_failure_before_commit_rolls_back_and_a_retry_establishes(database, monkeypatch):
    def failing_audit(*args, **kwargs):
        raise RuntimeError("injected audit failure")
    monkeypatch.setattr(admin, "audit", failing_audit)
    with pytest.raises(RuntimeError, match="injected"):
        establish_initial_workshop(database, **request())
    for table in ("repair_organizations", "workshops", "workshop_audit_events"):
        assert rows(database, f"SELECT count(*) FROM {table}") == [(0,)]
    monkeypatch.setattr(admin, "audit", audit)
    assert establish_initial_workshop(database, **request())["status"] == "ESTABLISHED"


def test_response_lost_after_commit_is_safely_retried(database, monkeypatch, capsys):
    def lost_output(*args, **kwargs):
        raise OSError("operator terminal disconnected")
    monkeypatch.setattr(admin, "print", lost_output, raising=False)
    with pytest.raises(OSError, match="disconnected"):
        run_cli(monkeypatch, *cli_arguments(database))
    committed = rows(database, "SELECT event_id FROM workshop_audit_events")
    assert len(committed) == 1 and rows(database, "SELECT count(*) FROM workshops") == [(1,)]
    monkeypatch.delattr(admin, "print")
    run_cli(monkeypatch, *cli_arguments(database))
    result = json.loads(capsys.readouterr().out)
    assert result == {"status": "ALREADY_ESTABLISHED", "organization_id": ORGANIZATION,
                      "workshop_id": WORKSHOP, "audit_event_id": committed[0][0]}
    assert rows(database, "SELECT count(*) FROM workshop_audit_events") == [(1,)]


def test_concurrent_identical_requests_establish_exactly_once(database):
    barrier, results, errors = threading.Barrier(2), [], []

    def establish():
        try:
            barrier.wait()
            results.append(establish_initial_workshop(database, **request()))
        except Exception as error:  # surfaced below
            errors.append(error)

    threads = [threading.Thread(target=establish) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not errors
    assert sorted(result["status"] for result in results) == ["ALREADY_ESTABLISHED", "ESTABLISHED"]
    assert len({result["audit_event_id"] for result in results}) == 1
    assert rows(database, "SELECT count(*) FROM workshop_audit_events") == [(1,)]


def test_establishment_then_membership_resolves_exactly_this_single_active_workshop(tmp_path, database):
    establish_initial_workshop(database, **request())
    service, directory = auth_directory(tmp_path, database)
    user = service.provision_identity(username="establishment.member", display_name="Fictional member",
                                      role=UserRole.OWNER, password=PASSWORD)
    assert rows(database, "SELECT count(*) FROM workshop_memberships") == [(0,)]
    directory.grant_membership(user.user_id, WORKSHOP, operator_reference="Explicit fictional membership")
    context = directory.resolve(user.user_id)
    assert context["workshop_id"] == WORKSHOP and context["organization_id"] == ORGANIZATION
    assert context["membership_version"] == 1
    assert context["auth_store_id"] == service.repository.store_id()
    assert rows(database, "SELECT count(*) FROM active_user_workshops") == [(0,)]  # derived, not selected


def test_openclaw_and_unknown_accounts_cannot_join_a_valid_established_workshop(tmp_path, database):
    establish_initial_workshop(database, **request())
    service, directory = auth_directory(tmp_path, database)
    robot = service.provision_identity(username="establishment.service", display_name="Fictional service",
                                       role=UserRole.OPENCLAW)
    for user_id in (robot.user_id, uuid4()):
        with pytest.raises(WorkshopAccessDenied, match="existing enabled human account"):
            directory.grant_membership(user_id, WORKSHOP, operator_reference="Explicit fictional membership")
    for table in ("workshop_principals", "workshop_memberships", "active_user_workshops"):
        assert rows(database, f"SELECT count(*) FROM {table}") == [(0,)]


def test_cli_arguments_and_results(tmp_path, database, monkeypatch, capsys):
    run_cli(monkeypatch, *cli_arguments(database))
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "ESTABLISHED" and (first["organization_id"], first["workshop_id"]) == (
        ORGANIZATION, WORKSHOP)
    run_cli(monkeypatch, *cli_arguments(database, operator_reference="Retry reference"))
    assert json.loads(capsys.readouterr().out) == first | {"status": "ALREADY_ESTABLISHED"}
    missing = tmp_path / "missing.db"
    for arguments in (cli_arguments(missing), cli_arguments("relative.db"),
                      cli_arguments(database)[:-2], cli_arguments(database) + ["--auth-database", str(missing)]):
        with pytest.raises(SystemExit) as exit_info:
            run_cli(monkeypatch, *arguments)
        assert exit_info.value.code == 2
    assert not missing.exists()
    with pytest.raises(WorkshopEstablishmentError, match="conflicts"):
        run_cli(monkeypatch, *cli_arguments(database, workshop_name="Other workshop"))
