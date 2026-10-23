"""Autodata 1B persistence: stored lookups, staff acceptance and history.

Every method runs in one WorkRepository.operation transaction and performs no
adapter or other external I/O. The lookup service (1B-2) recognises a stored
retry first, then calls prepare_lookup, runs the adapter outside any
transaction and finally calls store_lookup. Acceptance is FOUND evidence
accepted by a person; it is neither billing approval nor a quotation.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from domain.models.labour_time import LabourTime, LabourTimeSource
from integrations.autodata.models import LabourTimeLookupResult, LabourTimeRequest, LookupOutcome
from webapp.autodata_migrations import CONFIRMATION
from webapp.autodata_records import (
    ACCEPT_CONTRACT, MAX_WORK_DESCRIPTION, AcceptLookupCommand, canonical_json, canonical_uuid,
    command_digest, digest, identity_json, minimise_result, parse_utc,
    search_text_has_identifier, snapshot_has_identifier, split_request, utc_micro,
)
from webapp.database import IdempotencyConflictError, WorkRepository
from webapp.labour_models import decimal_storage_text
from webapp.labour_snapshot import build_labour_snapshot
from webapp.workshop_context import TrustedWorkshopContext, WorkshopPermission


ACCEPTANCE_WINDOW = timedelta(hours=24)
EVENT_TYPE = "AUTODATA_HAKU_HYVAKSYTTY"
PAGE_LIMIT = 20


class AutodataError(Exception):
    code = "AUTODATA_ERROR"


class LookupNotFoundError(AutodataError):
    code = "LOOKUP_NOT_FOUND"


class AutodataClosedError(AutodataError):
    code = "AUTODATA_CLOSED"


class StaleViewError(AutodataError):
    code = "STALE_VIEW"


class StaleLookupError(AutodataError):
    code = "STALE_LOOKUP"


class ExpiredLookupError(AutodataError):
    code = "EXPIRED"


class InvalidLookupTimeError(AutodataError):
    code = "INVALID_LOOKUP_TIME"


class NotAcceptableError(AutodataError):
    code = "NOT_ACCEPTABLE"


class SyntheticNotAcceptableError(AutodataError):
    code = "SYNTHETIC_NOT_ACCEPTABLE"


class SelectionMismatchError(AutodataError):
    code = "SELECTION_MISMATCH"


class AlreadyAcceptedError(AutodataError):
    code = "ALREADY_ACCEPTED"


class SelectionTooLongError(AutodataError):
    code = "SELECTION_TOO_LONG"


class IdentifierInSearchTextError(AutodataError):
    code = "IDENTIFIER_IN_SEARCH_TEXT"


class IdentifierInRequestError(AutodataError):
    """Registration or VIN in vehicle facts or selection paths (immutable snapshot)."""

    code = "IDENTIFIER_IN_REQUEST"


@dataclass(frozen=True)
class LookupPreparation:
    registration: str
    source_work_version: int


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _attribution(context: TrustedWorkshopContext) -> dict:
    if context.actor is not None:
        return context.actor.attribution()
    # Explicit legacy offline scopes only; HTTP contexts always carry an actor.
    return {"version": 1, "provenance": "offline_trusted_context", "employee_id": context.employee_id}


def _require_scope(context: TrustedWorkshopContext) -> None:
    if not isinstance(context, TrustedWorkshopContext) or not context.auth_store_id:
        raise PermissionError("a trusted workshop context with an auth store is required")


class AutodataRepository:
    def __init__(self, work_repository: WorkRepository, *, clock=_utc_now):
        self.work = work_repository
        self.clock = clock

    # -- shared reads -----------------------------------------------------

    @staticmethod
    def _is_closed(connection, inquiry_id: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM autodata_inquiry_closures WHERE inquiry_id = ?", (inquiry_id,),
        ).fetchone() is not None

    @staticmethod
    def _stored_lookup(connection, context, request_id: str, inquiry_id: str, command_sha256: str):
        """A stored retry of this user's request, or None; any mismatch conflicts."""
        row = connection.execute(
            "SELECT * FROM autodata_lookups WHERE auth_store_id = ? AND requested_by = ? "
            "AND workshop_id = ? AND request_id = ?",
            (context.auth_store_id, context.employee_id, context.workshop_id, request_id),
        ).fetchone()
        if row is None:
            return None
        # A redacted lookup has no digest left; its retry can no longer match.
        if row["command_sha256"] != command_sha256 or row["inquiry_id"] != inquiry_id:
            raise IdempotencyConflictError(request_id)
        return row

    @staticmethod
    def _lookup_record(row) -> dict:
        return {
            "lookup_id": row["lookup_id"], "inquiry_id": row["inquiry_id"],
            "request_id": row["request_id"], "source_work_version": row["source_work_version"],
            "requested_at": row["requested_at"], "retrieved_at": row["retrieved_at"],
            "expires_at": utc_micro(parse_utc(row["retrieved_at"]) + ACCEPTANCE_WINDOW),
            "outcome": row["outcome"], "hours": row["hours"], "raw_time_text": row["raw_time_text"],
            "result_sha256": row["result_sha256"], "synthetic": bool(row["synthetic"]),
            "adapter_name": row["adapter_name"], "adapter_version": row["adapter_version"],
            "requested_by_attribution": json.loads(row["requested_by_attribution_json"]),
            "result": json.loads(row["result_json"]),
        }

    # -- lookup -----------------------------------------------------------

    def find_stored_lookup(self, context, *, inquiry_id: str, request_id: str,
                           command_sha256: str) -> dict | None:
        """Retry recognition: current scope only, no current-version check (plan 3.2/1)."""
        _require_scope(context)
        request_id = canonical_uuid(request_id)
        with self.work.operation(context, WorkshopPermission.SAVE_LABOUR_PLAN) as connection:
            self.work._find_inquiry(connection, inquiry_id, context)
            row = self._stored_lookup(connection, context, request_id, inquiry_id, command_sha256)
            return None if row is None else self._lookup_record(row)

    def prepare_lookup(self, context, *, inquiry_id: str, expected_version: int,
                       search_text: str, vin: str | None) -> LookupPreparation:
        """New-lookup checks only; the caller has already ruled out a stored retry."""
        _require_scope(context)
        with self.work.operation(context, WorkshopPermission.SAVE_LABOUR_PLAN) as connection:
            inquiry = self.work._find_inquiry(connection, inquiry_id, context)
            if self._is_closed(connection, inquiry_id):
                raise AutodataClosedError(inquiry_id)
            if inquiry["version"] != expected_version:
                raise StaleViewError(inquiry_id)
            if search_text_has_identifier(search_text, registration=inquiry["registration"], vin=vin):
                raise IdentifierInSearchTextError(inquiry_id)
            return LookupPreparation(inquiry["registration"], inquiry["version"])

    def store_lookup(self, context, *, inquiry_id: str, request_id: str, command_sha256: str,
                     request: LabourTimeRequest, result: LabourTimeLookupResult,
                     source_work_version: int, requested_at: datetime,
                     retrieved_at: datetime) -> dict:
        """Store one immutable lookup; retrieved_at alone governs acceptance age."""
        _require_scope(context)
        request_id = canonical_uuid(request_id)
        snapshot, inputs = split_request(request)
        with self.work.operation(context, WorkshopPermission.SAVE_LABOUR_PLAN, write=True) as connection:
            inquiry = self.work._find_inquiry(connection, inquiry_id, context)
            stored = self._stored_lookup(connection, context, request_id, inquiry_id, command_sha256)
            if stored is not None:
                return self._lookup_record(stored)
            # Closed while the adapter ran: discard the result, store nothing.
            if self._is_closed(connection, inquiry_id):
                raise AutodataClosedError(inquiry_id)
            if inputs["registration"] != inquiry["registration"]:
                raise ValueError("the lookup registration must come from the inquiry")
            if search_text_has_identifier(inputs["search_text"], registration=inquiry["registration"],
                                          vin=inputs["vin"]):
                raise IdentifierInSearchTextError(inquiry_id)
            # Vehicle facts and replayed selection paths enter immutable JSON as text.
            if snapshot_has_identifier(snapshot, registration=inquiry["registration"], vin=inputs["vin"]):
                raise IdentifierInRequestError(inquiry_id)
            result = minimise_result(result, registration=inquiry["registration"], vin=inputs["vin"])
            result_value = result.model_dump(mode="json")
            found = result.outcome == LookupOutcome.FOUND
            lookup_id = str(uuid4())
            connection.execute(
                """INSERT INTO autodata_lookups (
                    lookup_id, organization_id, workshop_id, inquiry_id, auth_store_id, requested_by,
                    requested_by_attribution_json, request_id, command_sha256, snapshot_json,
                    snapshot_sha256, registration_source, source_work_version, requested_at,
                    retrieved_at, outcome, result_json, result_sha256, hours, raw_time_text,
                    vehicle_identity_json, operation_identity_json, variant_identity_json,
                    adapter_name, adapter_version, synthetic
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?, 'INQUIRY', ?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    lookup_id, context.organization_id, context.workshop_id, inquiry_id,
                    context.auth_store_id, context.employee_id, canonical_json(_attribution(context)),
                    request_id, command_sha256, canonical_json(snapshot),
                    digest(request.model_dump(mode="json")), source_work_version,
                    utc_micro(requested_at), utc_micro(retrieved_at), result.outcome.value,
                    canonical_json(result_value), digest(result_value),
                    decimal_storage_text(result.time.hours) if found else None,
                    result.time.raw_time_text if found else None,
                    identity_json(result.vehicle.choice.identity) if result.vehicle else None,
                    identity_json(result.operation.choice.identity) if result.operation else None,
                    identity_json(result.variant.choice.identity) if result.variant else None,
                    result.provenance.adapter_name, result.provenance.adapter_version,
                    int(result.provenance.synthetic),
                ),
            )
            connection.execute(
                "INSERT INTO autodata_lookup_vehicle_inputs "
                "(lookup_id, inquiry_id, workshop_id, registration, vin, search_text) VALUES (?,?,?,?,?,?)",
                (lookup_id, inquiry_id, context.workshop_id, inputs["registration"], inputs["vin"],
                 inputs["search_text"]),
            )
            row = connection.execute("SELECT * FROM autodata_lookups WHERE lookup_id = ?",
                                     (lookup_id,)).fetchone()
            return self._lookup_record(row)

    # -- acceptance -------------------------------------------------------

    @staticmethod
    def _acceptability(row, *, inquiry_version, accepted, now, allow_synthetic) -> str:
        if accepted:
            return "ACCEPTED"
        if row["outcome"] != LookupOutcome.FOUND.value:
            return "NOT_FOUND_OUTCOME"
        if row["synthetic"] and not allow_synthetic:
            return "SYNTHETIC"
        if inquiry_version != row["source_work_version"]:
            return "STALE_LOOKUP"
        age = now - parse_utc(row["retrieved_at"])
        if age < timedelta(0):
            return "INVALID_LOOKUP_TIME"
        return "ACCEPTABLE" if age < ACCEPTANCE_WINDOW else "EXPIRED"

    def accept_lookup(self, context, *, inquiry_id: str, lookup_id: str,
                      command: AcceptLookupCommand, allow_synthetic: bool = False) -> dict:
        """Decision, labour, event, new version and receipt in ONE transaction (plan 3.3)."""
        _require_scope(context)
        command = AcceptLookupCommand.model_validate(command.model_dump())
        # Path and body only; the receipt key lacks inquiry and lookup, so they are hashed.
        command_sha256 = command_digest(
            ACCEPT_CONTRACT, actor_user_id=context.employee_id, inquiry_id=inquiry_id,
            body=command.body(), lookup_id=lookup_id,
        )
        with self.work.operation(context, WorkshopPermission.SAVE_LABOUR_PLAN, write=True) as connection:
            # 1. Retry first: same decision and version, even after expiry or later changes.
            receipt = connection.execute(
                "SELECT command_sha256, response_json FROM autodata_decision_receipts "
                "WHERE auth_store_id = ? AND employee_id = ? AND workshop_id = ? AND request_id = ?",
                (context.auth_store_id, context.employee_id, context.workshop_id, command.request_id),
            ).fetchone()
            if receipt is not None:
                if receipt["command_sha256"] != command_sha256:
                    raise IdempotencyConflictError(command.request_id)
                self.work._find_inquiry(connection, inquiry_id, context)
                return {"decision": json.loads(receipt["response_json"]), "replayed": True,
                        "work": self.work._read_work(connection, inquiry_id, context)}
            # 2. Scope, then closure BEFORE reading the lookup (a closure deletes it).
            inquiry = self.work._find_inquiry(connection, inquiry_id, context)
            if self._is_closed(connection, inquiry_id):
                raise AutodataClosedError(inquiry_id)
            # 3. Lookup of this workshop and inquiry only; anything else is "not found".
            lookup = connection.execute(
                "SELECT * FROM autodata_lookups WHERE lookup_id = ? AND inquiry_id = ? "
                "AND workshop_id = ? AND organization_id = ?",
                (lookup_id, inquiry_id, context.workshop_id, context.organization_id),
            ).fetchone()
            if lookup is None:
                raise LookupNotFoundError(lookup_id)
            if lookup["outcome"] != LookupOutcome.FOUND.value:
                raise NotAcceptableError(lookup_id)
            if lookup["synthetic"] and not allow_synthetic:
                raise SyntheticNotAcceptableError(lookup_id)
            if (command.expected_result_sha256 != lookup["result_sha256"]
                    or identity_json(command.vehicle_identity) != lookup["vehicle_identity_json"]
                    or identity_json(command.operation_identity) != lookup["operation_identity_json"]
                    or identity_json(command.variant_identity) != lookup["variant_identity_json"]):
                raise SelectionMismatchError(lookup_id)
            # 8. The clock is read only now, after BEGIN IMMEDIATE has the write lock.
            now = self.clock()
            age = now - parse_utc(lookup["retrieved_at"])
            if age < timedelta(0):
                raise InvalidLookupTimeError(lookup_id)
            if age >= ACCEPTANCE_WINDOW:
                raise ExpiredLookupError(lookup_id)
            if not (inquiry["version"] == lookup["source_work_version"] == command.expected_version):
                raise StaleLookupError(lookup_id)
            if connection.execute("SELECT 1 FROM autodata_labour_decisions WHERE lookup_id = ?",
                                  (lookup_id,)).fetchone() is not None:
                raise AlreadyAcceptedError(lookup_id)
            # 11. Hours come from the stored FOUND lookup, never from the request.
            description = " / ".join(
                command.operation_identity.selection_path + command.variant_identity.selection_path)
            if len(description) > MAX_WORK_DESCRIPTION:
                raise SelectionTooLongError(lookup_id)
            labour_time = LabourTime(
                hours=Decimal(lookup["hours"]), source=LabourTimeSource.AUTODATA,
                work_description=description, autodata_reference=f"lookup:{lookup_id}",
                recorded_by=context.employee_id,
            )
            snapshot = build_labour_snapshot(labour_time)
            decision_id = str(uuid4())
            attribution = _attribution(context)
            resulting_version = lookup["source_work_version"] + 1
            details = dict(snapshot)
            details["actor_attribution"] = attribution
            details["autodata"] = {
                "lookup_id": lookup_id, "decision_id": decision_id,
                "vehicle_identity": command.vehicle_identity.model_dump(mode="json"),
                "operation_identity": command.operation_identity.model_dump(mode="json"),
                "variant_identity": command.variant_identity.model_dump(mode="json"),
                "raw_time_text": lookup["raw_time_text"], "result_sha256": lookup["result_sha256"],
                "retrieved_at": lookup["retrieved_at"], "adapter_name": lookup["adapter_name"],
                "adapter_version": lookup["adapter_version"], "synthetic": bool(lookup["synthetic"]),
                "confirmation": CONFIRMATION,
            }
            event_id = self.work._apply_labour(
                connection, work_id=inquiry_id, expected_version=lookup["source_work_version"],
                labour=snapshot, details=details, event_type=EVENT_TYPE,
                occurred_at=now.astimezone(timezone.utc).isoformat(timespec="seconds"),
            )
            decided_at = utc_micro(now)
            connection.execute(
                """INSERT INTO autodata_labour_decisions (
                    decision_id, lookup_id, organization_id, workshop_id, inquiry_id, decision,
                    vehicle_identity_json, operation_identity_json, variant_identity_json, hours,
                    raw_time_text, result_sha256, confirmation, source_work_version,
                    resulting_work_version, lookup_retrieved_at, decided_at, decided_by,
                    decided_by_attribution_json, labour_snapshot_json, work_event_id
                ) VALUES (?,?,?,?,?, 'ACCEPTED', ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    decision_id, lookup_id, context.organization_id, context.workshop_id, inquiry_id,
                    lookup["vehicle_identity_json"], lookup["operation_identity_json"],
                    lookup["variant_identity_json"], lookup["hours"], lookup["raw_time_text"],
                    lookup["result_sha256"], CONFIRMATION, lookup["source_work_version"],
                    resulting_version, lookup["retrieved_at"], decided_at, context.employee_id,
                    canonical_json(attribution), canonical_json(snapshot), event_id,
                ),
            )
            response = {"decision_id": decision_id, "inquiry_id": inquiry_id, "lookup_id": lookup_id,
                        "resulting_work_version": resulting_version, "decided_at": decided_at}
            connection.execute(
                """INSERT INTO autodata_decision_receipts (
                    auth_store_id, employee_id, workshop_id, request_id, inquiry_id, lookup_id,
                    command_sha256, decision_id, response_json, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (context.auth_store_id, context.employee_id, context.workshop_id, command.request_id,
                 inquiry_id, lookup_id, command_sha256, decision_id, canonical_json(response),
                 decided_at),
            )
            return {"decision": response, "replayed": False,
                    "work": self.work._read_work(connection, inquiry_id, context)}

    # -- history ----------------------------------------------------------

    @staticmethod
    def _limit(limit: int) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= PAGE_LIMIT:
            raise ValueError(f"limit must be 1..{PAGE_LIMIT}")
        return limit

    def list_lookups(self, context, *, inquiry_id: str, limit: int = PAGE_LIMIT,
                     before: tuple[str, str] | None = None, allow_synthetic: bool = False) -> dict:
        """Newest first; summaries only (no candidates), with server acceptability."""
        limit = self._limit(limit)
        with self.work.operation(context, WorkshopPermission.READ_LABOUR_PLAN) as connection:
            inquiry = self.work._find_inquiry(connection, inquiry_id, context)
            query = ("SELECT l.*, d.decision_id FROM autodata_lookups l "
                     "LEFT JOIN autodata_labour_decisions d ON d.lookup_id = l.lookup_id "
                     "WHERE l.inquiry_id = ? AND l.workshop_id = ?")
            parameters = [inquiry_id, context.workshop_id]
            if before is not None:
                query += " AND (l.retrieved_at, l.lookup_id) < (?, ?)"
                parameters += list(before)
            query += " ORDER BY l.retrieved_at DESC, l.lookup_id DESC LIMIT ?"
            rows = connection.execute(query, (*parameters, limit + 1)).fetchall()
            now = self.clock()
            items = []
            for row in rows[:limit]:
                result = json.loads(row["result_json"])
                items.append({
                    "lookup_id": row["lookup_id"], "outcome": row["outcome"], "hours": row["hours"],
                    "raw_time_text": row["raw_time_text"], "retrieved_at": row["retrieved_at"],
                    "expires_at": utc_micro(parse_utc(row["retrieved_at"]) + ACCEPTANCE_WINDOW),
                    "source_work_version": row["source_work_version"],
                    "synthetic": bool(row["synthetic"]), "decision_id": row["decision_id"],
                    "labels": {name: (result.get(name) or {}).get("choice", {}).get("identity", {})
                               .get("selection_path") for name in ("vehicle", "operation", "variant")},
                    "acceptability": self._acceptability(
                        row, inquiry_version=inquiry["version"], accepted=row["decision_id"] is not None,
                        now=now, allow_synthetic=allow_synthetic),
                })
            more = len(rows) > limit
            last = rows[limit - 1] if more else None
            return {"items": items,
                    "next": (last["retrieved_at"], last["lookup_id"]) if last is not None else None}

    def get_lookup(self, context, *, inquiry_id: str, lookup_id: str) -> dict:
        with self.work.operation(context, WorkshopPermission.READ_LABOUR_PLAN) as connection:
            self.work._find_inquiry(connection, inquiry_id, context)
            row = connection.execute(
                "SELECT * FROM autodata_lookups WHERE lookup_id = ? AND inquiry_id = ? AND workshop_id = ?",
                (lookup_id, inquiry_id, context.workshop_id),
            ).fetchone()
            if row is None:
                raise LookupNotFoundError(lookup_id)
            return self._lookup_record(row)

    def list_decisions(self, context, *, inquiry_id: str, limit: int = PAGE_LIMIT,
                       before: tuple[str, str] | None = None) -> dict:
        limit = self._limit(limit)
        with self.work.operation(context, WorkshopPermission.READ_LABOUR_PLAN) as connection:
            self.work._find_inquiry(connection, inquiry_id, context)
            query = "SELECT * FROM autodata_labour_decisions WHERE inquiry_id = ? AND workshop_id = ?"
            parameters = [inquiry_id, context.workshop_id]
            if before is not None:
                query += " AND (decided_at, decision_id) < (?, ?)"
                parameters += list(before)
            query += " ORDER BY decided_at DESC, decision_id DESC LIMIT ?"
            rows = connection.execute(query, (*parameters, limit + 1)).fetchall()
            items = [{
                "decision_id": row["decision_id"], "lookup_id": row["lookup_id"],
                "hours": row["hours"], "raw_time_text": row["raw_time_text"],
                "source_work_version": row["source_work_version"],
                "resulting_work_version": row["resulting_work_version"],
                "decided_at": row["decided_at"], "decided_by": row["decided_by"],
                "decided_by_attribution": json.loads(row["decided_by_attribution_json"]),
                "vehicle_identity": json.loads(row["vehicle_identity_json"]),
                "operation_identity": json.loads(row["operation_identity_json"]),
                "variant_identity": json.loads(row["variant_identity_json"]),
            } for row in rows[:limit]]
            more = len(rows) > limit
            last = rows[limit - 1] if more else None
            return {"items": items,
                    "next": (last["decided_at"], last["decision_id"]) if last is not None else None}
