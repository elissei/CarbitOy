"""Autodata 1B persistence: stored lookups, staff acceptance, removal and history.

Every method runs in one WorkRepository.operation transaction and performs no
adapter or other external I/O. The lookup service (1B-2) recognises a stored
retry first, then calls prepare_lookup, runs the adapter outside any
transaction and finally calls store_lookup. Acceptance is FOUND evidence
accepted by a person; it is neither billing approval nor a quotation.

Since 1B-2.0 (migration 2) a lookup is an immutable body of closed values plus
one deletable payload holding all external and staff text and its digests.
Acceptance copies no external text: the work description is empty and the
event carries closed fields only. Redaction deletes one payload; closure
deletes an inquiry's Autodata evidence except the receipts (P7).
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from domain.models.labour_time import LabourTime, LabourTimeSource
from integrations.autodata.models import LabourTimeLookupResult, LabourTimeRequest, LookupOutcome
from webapp.autodata_migrations import (
    ADAPTERS, CLOSE_CONFIRMATION, CLOSURE_REASONS, CONFIRMATION, REDACTION_REASONS,
)
from webapp.autodata_records import (
    AcceptLookupCommand, canonical_json, canonical_uuid, closed_body, digest, identity_json,
    minimise_result, parse_utc, search_text_has_identifier, snapshot_has_identifier, split_request,
    utc_micro,
)
from webapp.database import IdempotencyConflictError, WorkRepository
from webapp.labour_models import decimal_storage_text
from webapp.labour_snapshot import build_labour_snapshot
from webapp.workshop_context import TrustedWorkshopContext, WorkshopPermission


ACCEPTANCE_WINDOW = timedelta(hours=24)
EVENT_TYPE = "AUTODATA_HAKU_HYVAKSYTTY"
PAGE_LIMIT = 20
# Deleted by redaction or closure, in dependency order (children first).
REMOVABLE_TABLES = ("autodata_labour_decisions", "autodata_redaction_events",
                    "autodata_lookup_payloads", "autodata_lookups")


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


class AlreadyAcceptedError(AutodataError):
    code = "ALREADY_ACCEPTED"


class RequestRedactedError(AutodataError):
    """A retried lookup request whose payload, and so its comparison digest, was redacted."""

    code = "REQUEST_REDACTED"


class LookupRedactedError(AutodataError):
    code = "LOOKUP_REDACTED"


class AlreadyRedactedError(AutodataError):
    code = "ALREADY_REDACTED"


class AutodataDisabledError(AutodataError):
    """Autodata is disabled: only stored lookups and acceptance receipts are replayed."""

    code = "AUTODATA_DISABLED"


class AutodataIntegrityError(AutodataError):
    """A payload missing without its redaction event, or rows left by a removal."""

    code = "AUTODATA_INTEGRITY"


class IdentifierInSearchTextError(AutodataError):
    code = "IDENTIFIER_IN_SEARCH_TEXT"


class IdentifierInRequestError(AutodataError):
    """Registration or VIN in vehicle facts or selection paths."""

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


def _labels(result: dict | None) -> dict | None:
    if result is None:
        return None
    return {name: (result.get(name) or {}).get("choice", {}).get("identity", {}).get("selection_path")
            for name in ("vehicle", "operation", "variant")}


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
    def _is_redacted(connection, lookup_id: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM autodata_redaction_events WHERE lookup_id = ?", (lookup_id,),
        ).fetchone() is not None

    @staticmethod
    def _payload(connection, lookup_id: str):
        return connection.execute(
            "SELECT * FROM autodata_lookup_payloads WHERE lookup_id = ?", (lookup_id,),
        ).fetchone()

    def _payload_or_redacted(self, connection, lookup_id: str):
        """The payload, or None after a logged redaction; never silently missing."""
        payload = self._payload(connection, lookup_id)
        if payload is None and not self._is_redacted(connection, lookup_id):
            raise AutodataIntegrityError(lookup_id)
        return payload

    def _stored_lookup(self, connection, context, request_id: str, inquiry_id: str, command_sha256: str):
        """This user's stored request as (body, payload), or None; a mismatch conflicts."""
        body = connection.execute(
            "SELECT * FROM autodata_lookups WHERE auth_store_id = ? AND requested_by = ? "
            "AND workshop_id = ? AND request_id = ?",
            (context.auth_store_id, context.employee_id, context.workshop_id, request_id),
        ).fetchone()
        if body is None:
            return None
        if body["inquiry_id"] != inquiry_id:
            raise IdempotencyConflictError(request_id)
        payload = self._payload_or_redacted(connection, body["lookup_id"])
        if payload is None:
            # The comparison digest left with the payload: never call the adapter again.
            raise RequestRedactedError(request_id)
        if payload["command_sha256"] != command_sha256:
            raise IdempotencyConflictError(request_id)
        return body, payload

    @staticmethod
    def _lookup_record(body, payload) -> dict:
        """The body always; payload texts only while the payload exists."""
        return {
            "lookup_id": body["lookup_id"], "inquiry_id": body["inquiry_id"],
            "request_id": body["request_id"], "source_work_version": body["source_work_version"],
            "requested_at": body["requested_at"], "retrieved_at": body["retrieved_at"],
            "expires_at": utc_micro(parse_utc(body["retrieved_at"]) + ACCEPTANCE_WINDOW),
            "outcome": body["outcome"], "level": body["level"], "candidate_count": body["candidate_count"],
            "hours": body["hours"], "time_text": body["time_text"], "rejection_code": body["rejection_code"],
            "synthetic": bool(body["synthetic"]),
            "adapter_name": body["adapter_name"], "adapter_version": body["adapter_version"],
            "requested_by_attribution": json.loads(body["requested_by_attribution_json"]),
            "redacted": payload is None,
            "raw_time_text": payload["raw_time_text"] if payload is not None else None,
            "result": json.loads(payload["result_json"]) if payload is not None else None,
        }

    # -- lookup -----------------------------------------------------------

    def find_stored_lookup(self, context, *, inquiry_id: str, request_id: str,
                           command_sha256: str) -> dict | None:
        """Retry recognition: authorization, then scope, then the stored request (plan 13.1).

        No current-version check; a redacted request raises REQUEST_REDACTED.
        """
        _require_scope(context)
        request_id = canonical_uuid(request_id)
        with self.work.operation(context, WorkshopPermission.SAVE_LABOUR_PLAN) as connection:
            self.work._find_inquiry(connection, inquiry_id, context)
            stored = self._stored_lookup(connection, context, request_id, inquiry_id, command_sha256)
            return None if stored is None else self._lookup_record(*stored)

    def prepare_lookup(self, context, *, inquiry_id: str, expected_version: int,
                       search_text: str, vin: str | None) -> LookupPreparation:
        """New-lookup checks: authorization -> scope -> closure -> version (plan 13.1).

        A closed inquiry answers AUTODATA_CLOSED even for an old expected_version,
        so the adapter is never called for it.
        """
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
        """Store one immutable body and its payload; retrieved_at alone governs acceptance age."""
        _require_scope(context)
        request_id = canonical_uuid(request_id)
        if (result.provenance.adapter_name, result.provenance.adapter_version) not in ADAPTERS:
            raise ValueError("unknown autodata adapter; a new adapter requires a schema migration")
        snapshot, inputs = split_request(request)
        with self.work.operation(context, WorkshopPermission.SAVE_LABOUR_PLAN, write=True) as connection:
            inquiry = self.work._find_inquiry(connection, inquiry_id, context)
            stored = self._stored_lookup(connection, context, request_id, inquiry_id, command_sha256)
            if stored is not None:
                return self._lookup_record(*stored)
            # Closed while the adapter ran: discard the result, store nothing.
            if self._is_closed(connection, inquiry_id):
                raise AutodataClosedError(inquiry_id)
            if inputs["registration"] != inquiry["registration"]:
                raise ValueError("the lookup registration must come from the inquiry")
            if search_text_has_identifier(inputs["search_text"], registration=inquiry["registration"],
                                          vin=inputs["vin"]):
                raise IdentifierInSearchTextError(inquiry_id)
            if snapshot_has_identifier(snapshot, registration=inquiry["registration"], vin=inputs["vin"]):
                raise IdentifierInRequestError(inquiry_id)
            result = minimise_result(result, registration=inquiry["registration"], vin=inputs["vin"])
            result, closed = closed_body(result)
            result_value = result.model_dump(mode="json")
            found = result.outcome == LookupOutcome.FOUND
            lookup_id = str(uuid4())
            connection.execute(
                """INSERT INTO autodata_lookups (
                    lookup_id, organization_id, workshop_id, inquiry_id, auth_store_id, requested_by,
                    requested_by_attribution_json, request_id, registration_source, source_work_version,
                    requested_at, retrieved_at, outcome, level, candidate_count, hours, time_text,
                    rejection_code, adapter_name, adapter_version, synthetic
                ) VALUES (?,?,?,?,?,?,?,?, 'INQUIRY', ?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    lookup_id, context.organization_id, context.workshop_id, inquiry_id,
                    context.auth_store_id, context.employee_id, canonical_json(_attribution(context)),
                    request_id, source_work_version, utc_micro(requested_at), utc_micro(retrieved_at),
                    closed.outcome, closed.level, closed.candidate_count,
                    decimal_storage_text(closed.hours) if found else None, closed.time_text,
                    closed.rejection_code, result.provenance.adapter_name,
                    result.provenance.adapter_version, int(result.provenance.synthetic),
                ),
            )
            connection.execute(
                """INSERT INTO autodata_lookup_payloads (
                    lookup_id, inquiry_id, workshop_id, registration, vin, search_text,
                    request_snapshot_json, result_json, vehicle_identity_json, operation_identity_json,
                    variant_identity_json, raw_time_text, result_sha256, command_sha256, snapshot_sha256
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    lookup_id, inquiry_id, context.workshop_id, inputs["registration"], inputs["vin"],
                    inputs["search_text"], canonical_json(snapshot), canonical_json(result_value),
                    identity_json(result.vehicle.choice.identity) if result.vehicle else None,
                    identity_json(result.operation.choice.identity) if result.operation else None,
                    identity_json(result.variant.choice.identity) if result.variant else None,
                    result.time.raw_time_text if found else None, digest(result_value), command_sha256,
                    digest(request.model_dump(mode="json")),
                ),
            )
            body = connection.execute("SELECT * FROM autodata_lookups WHERE lookup_id = ?",
                                      (lookup_id,)).fetchone()
            return self._lookup_record(body, self._payload(connection, lookup_id))

    # -- acceptance -------------------------------------------------------

    @staticmethod
    def _acceptability(row, *, inquiry_version, accepted, redacted, now, allow_synthetic) -> str:
        if accepted:
            return "ACCEPTED"
        if redacted:
            return "REDACTED"
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

    @staticmethod
    def _receipt_response(receipt) -> dict:
        return {"decision_id": receipt["decision_id"], "inquiry_id": receipt["inquiry_id"],
                "lookup_id": receipt["lookup_id"], "resulting_work_version": receipt["resulting_work_version"],
                "decided_at": receipt["decided_at"]}

    def accept_lookup(self, context, *, inquiry_id: str, lookup_id: str,
                      command: AcceptLookupCommand, allow_synthetic: bool = False,
                      replay_only: bool = False) -> dict:
        """Decision, labour, event, new version and receipt in ONE transaction (plan 4, 13.3).

        One path for new acceptances and retries: authorization (operation) ->
        scope -> receipt -> closure -> body -> payload -> checks -> writes.
        replay_only (disabled Autodata) returns a matching receipt and refuses
        everything else with AUTODATA_DISABLED in the same transaction, writing nothing.
        """
        _require_scope(context)
        command = AcceptLookupCommand.model_validate(command.model_dump())
        with self.work.operation(context, WorkshopPermission.SAVE_LABOUR_PLAN, write=True) as connection:
            # 1. Scope before the receipt: another workshop's inquiry is "not found" (Codex 3).
            inquiry = self.work._find_inquiry(connection, inquiry_id, context)
            # 2. Retry: closed fields only; valid even after expiry, redaction or closure (P7).
            receipt = connection.execute(
                "SELECT * FROM autodata_decision_receipts "
                "WHERE auth_store_id = ? AND employee_id = ? AND workshop_id = ? AND request_id = ?",
                (context.auth_store_id, context.employee_id, context.workshop_id, command.request_id),
            ).fetchone()
            if receipt is not None:
                if (receipt["inquiry_id"], receipt["lookup_id"], receipt["expected_version"],
                        receipt["confirmation"]) != (inquiry_id, lookup_id, command.expected_version,
                                                     command.confirmation):
                    raise IdempotencyConflictError(command.request_id)
                return {"decision": self._receipt_response(receipt), "replayed": True,
                        "work": self.work._read_work(connection, inquiry_id, context)}
            if replay_only:
                raise AutodataDisabledError()
            # 3. Closure before reading the lookup (a closure deletes it).
            if self._is_closed(connection, inquiry_id):
                raise AutodataClosedError(inquiry_id)
            # 4. Body of this workshop and inquiry only; anything else is "not found".
            lookup = connection.execute(
                "SELECT * FROM autodata_lookups WHERE lookup_id = ? AND inquiry_id = ? "
                "AND workshop_id = ? AND organization_id = ?",
                (lookup_id, inquiry_id, context.workshop_id, context.organization_id),
            ).fetchone()
            if lookup is None:
                raise LookupNotFoundError(lookup_id)
            # 5. The payload is what the staff member saw; redacted evidence is not acceptable.
            if self._payload_or_redacted(connection, lookup_id) is None:
                raise LookupRedactedError(lookup_id)
            if lookup["outcome"] != LookupOutcome.FOUND.value:
                raise NotAcceptableError(lookup_id)
            if lookup["synthetic"] and not allow_synthetic:
                raise SyntheticNotAcceptableError(lookup_id)
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
            # 11. Hours from the body, never from the request; no source text (P2).
            labour_time = LabourTime(
                hours=Decimal(lookup["hours"]), source=LabourTimeSource.AUTODATA,
                work_description=None, autodata_reference=f"lookup:{lookup_id}",
                recorded_by=context.employee_id,
            )
            snapshot = build_labour_snapshot(labour_time)
            decision_id = str(uuid4())
            attribution = _attribution(context)
            resulting_version = lookup["source_work_version"] + 1
            details = dict(snapshot)
            details["actor_attribution"] = attribution
            # Exactly these closed fields; no identities, labels, raw text or digests.
            details["autodata"] = {
                "lookup_id": lookup_id, "decision_id": decision_id, "time_text": lookup["time_text"],
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
                    decision_id, lookup_id, organization_id, workshop_id, inquiry_id, decision, hours,
                    time_text, confirmation, source_work_version, resulting_work_version,
                    lookup_retrieved_at, decided_at, auth_store_id, decided_by,
                    decided_by_attribution_json, labour_snapshot_json, work_event_id
                ) VALUES (?,?,?,?,?, 'ACCEPTED', ?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    decision_id, lookup_id, context.organization_id, context.workshop_id, inquiry_id,
                    lookup["hours"], lookup["time_text"], CONFIRMATION, lookup["source_work_version"],
                    resulting_version, lookup["retrieved_at"], decided_at, context.auth_store_id,
                    context.employee_id, canonical_json(attribution), canonical_json(snapshot), event_id,
                ),
            )
            connection.execute(
                """INSERT INTO autodata_decision_receipts (
                    auth_store_id, employee_id, workshop_id, request_id, organization_id, inquiry_id,
                    lookup_id, expected_version, confirmation, decision_id, resulting_work_version,
                    decided_at, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (context.auth_store_id, context.employee_id, context.workshop_id, command.request_id,
                 context.organization_id, inquiry_id, lookup_id, command.expected_version,
                 command.confirmation, decision_id, resulting_version, decided_at, decided_at),
            )
            response = {"decision_id": decision_id, "inquiry_id": inquiry_id, "lookup_id": lookup_id,
                        "resulting_work_version": resulting_version, "decided_at": decided_at}
            return {"decision": response, "replayed": False,
                    "work": self.work._read_work(connection, inquiry_id, context)}

    # -- removal ----------------------------------------------------------

    def redact_lookup(self, context, *, inquiry_id: str, lookup_id: str, reason_code: str) -> dict:
        """Delete one lookup's payload after logging it; body, decision and receipt stay (P1, P4)."""
        _require_scope(context)
        if reason_code not in REDACTION_REASONS:
            raise ValueError("unknown redaction reason code")
        with self.work.operation(context, WorkshopPermission.SAVE_LABOUR_PLAN, write=True) as connection:
            self.work._find_inquiry(connection, inquiry_id, context)
            if self._is_closed(connection, inquiry_id):
                raise AutodataClosedError(inquiry_id)
            body = connection.execute(
                "SELECT lookup_id FROM autodata_lookups WHERE lookup_id = ? AND inquiry_id = ? "
                "AND workshop_id = ? AND organization_id = ?",
                (lookup_id, inquiry_id, context.workshop_id, context.organization_id),
            ).fetchone()
            if body is None:
                raise LookupNotFoundError(lookup_id)
            if self._is_redacted(connection, lookup_id):
                raise AlreadyRedactedError(lookup_id)
            if self._payload(connection, lookup_id) is None:
                raise AutodataIntegrityError(lookup_id)
            redaction_id, redacted_at = str(uuid4()), utc_micro(self.clock())
            connection.execute(
                """INSERT INTO autodata_redaction_events (
                    redaction_id, lookup_id, organization_id, inquiry_id, workshop_id, redacted_by,
                    redacted_by_attribution_json, redacted_at, reason_code
                ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (redaction_id, lookup_id, context.organization_id, inquiry_id, context.workshop_id,
                 context.employee_id, canonical_json(_attribution(context)), redacted_at, reason_code),
            )
            connection.execute("DELETE FROM autodata_lookup_payloads WHERE lookup_id = ?", (lookup_id,))
            if self._payload(connection, lookup_id) is not None:
                raise AutodataIntegrityError(lookup_id)
            return {"redaction_id": redaction_id, "lookup_id": lookup_id, "redacted_at": redacted_at}

    def close_autodata(self, context, *, inquiry_id: str, reason_code: str, confirmation: str) -> dict:
        """Permanently close an inquiry's Autodata evidence (P4, P7).

        Closure marker with row counts -> decisions -> redaction events ->
        payloads -> bodies, each deletion followed by its residual check (a
        left-over child would otherwise surface as a foreign-key error at the
        next step), all in one transaction. Receipts stay so that a successful
        acceptance retry still returns its decision.
        """
        _require_scope(context)
        if confirmation != CLOSE_CONFIRMATION:
            raise ValueError("closing Autodata evidence requires its explicit confirmation")
        if reason_code not in CLOSURE_REASONS:
            raise ValueError("unknown closure reason code")
        with self.work.operation(context, WorkshopPermission.APPROVE_LABOUR_PLAN, write=True) as connection:
            self.work._find_inquiry(connection, inquiry_id, context)
            if self._is_closed(connection, inquiry_id):
                raise AutodataClosedError(inquiry_id)
            row_counts = {
                table: connection.execute(f"SELECT count(*) FROM {table} WHERE inquiry_id = ?",
                                          (inquiry_id,)).fetchone()[0]
                for table in (*REMOVABLE_TABLES, "autodata_decision_receipts")
            }
            closed_at = utc_micro(self.clock())
            connection.execute(
                """INSERT INTO autodata_inquiry_closures (
                    inquiry_id, organization_id, workshop_id, closed_by, closed_by_attribution_json,
                    closed_at, reason_code, row_counts_json
                ) VALUES (?,?,?,?,?,?,?,?)""",
                (inquiry_id, context.organization_id, context.workshop_id, context.employee_id,
                 canonical_json(_attribution(context)), closed_at, reason_code, canonical_json(row_counts)),
            )
            for table in REMOVABLE_TABLES:
                connection.execute(f"DELETE FROM {table} WHERE inquiry_id = ?", (inquiry_id,))
                if connection.execute(f"SELECT 1 FROM {table} WHERE inquiry_id = ? LIMIT 1",
                                      (inquiry_id,)).fetchone() is not None:
                    raise AutodataIntegrityError(inquiry_id)
            return {"inquiry_id": inquiry_id, "closed_at": closed_at, "row_counts": row_counts}

    # -- history ----------------------------------------------------------

    @staticmethod
    def _limit(limit: int) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= PAGE_LIMIT:
            raise ValueError(f"limit must be 1..{PAGE_LIMIT}")
        return limit

    def list_lookups(self, context, *, inquiry_id: str, limit: int = PAGE_LIMIT,
                     before: tuple[str, str] | None = None, allow_synthetic: bool = False) -> dict:
        """Newest first; summaries only (no candidates), labels only while the payload exists."""
        limit = self._limit(limit)
        with self.work.operation(context, WorkshopPermission.READ_LABOUR_PLAN) as connection:
            inquiry = self.work._find_inquiry(connection, inquiry_id, context)
            query = ("SELECT l.*, d.decision_id, p.result_json, p.raw_time_text, "
                     "p.lookup_id IS NULL AS redacted FROM autodata_lookups l "
                     "LEFT JOIN autodata_labour_decisions d ON d.lookup_id = l.lookup_id "
                     "LEFT JOIN autodata_lookup_payloads p ON p.lookup_id = l.lookup_id "
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
                result = json.loads(row["result_json"]) if row["result_json"] is not None else None
                items.append({
                    "lookup_id": row["lookup_id"], "outcome": row["outcome"], "hours": row["hours"],
                    "time_text": row["time_text"], "raw_time_text": row["raw_time_text"],
                    "retrieved_at": row["retrieved_at"],
                    "expires_at": utc_micro(parse_utc(row["retrieved_at"]) + ACCEPTANCE_WINDOW),
                    "source_work_version": row["source_work_version"],
                    "synthetic": bool(row["synthetic"]), "decision_id": row["decision_id"],
                    "redacted": bool(row["redacted"]), "labels": _labels(result),
                    "acceptability": self._acceptability(
                        row, inquiry_version=inquiry["version"], accepted=row["decision_id"] is not None,
                        redacted=bool(row["redacted"]), now=now, allow_synthetic=allow_synthetic),
                })
            more = len(rows) > limit
            last = rows[limit - 1] if more else None
            return {"items": items,
                    "next": (last["retrieved_at"], last["lookup_id"]) if last is not None else None}

    def get_lookup(self, context, *, inquiry_id: str, lookup_id: str) -> dict:
        with self.work.operation(context, WorkshopPermission.READ_LABOUR_PLAN) as connection:
            self.work._find_inquiry(connection, inquiry_id, context)
            body = connection.execute(
                "SELECT * FROM autodata_lookups WHERE lookup_id = ? AND inquiry_id = ? AND workshop_id = ?",
                (lookup_id, inquiry_id, context.workshop_id),
            ).fetchone()
            if body is None:
                raise LookupNotFoundError(lookup_id)
            return self._lookup_record(body, self._payload_or_redacted(connection, lookup_id))

    def list_decisions(self, context, *, inquiry_id: str, limit: int = PAGE_LIMIT,
                       before: tuple[str, str] | None = None) -> dict:
        """Retained decision view (P1): labels only while the lookup's payload exists."""
        limit = self._limit(limit)
        with self.work.operation(context, WorkshopPermission.READ_LABOUR_PLAN) as connection:
            self.work._find_inquiry(connection, inquiry_id, context)
            query = ("SELECT d.*, p.result_json FROM autodata_labour_decisions d "
                     "LEFT JOIN autodata_lookup_payloads p ON p.lookup_id = d.lookup_id "
                     "WHERE d.inquiry_id = ? AND d.workshop_id = ?")
            parameters = [inquiry_id, context.workshop_id]
            if before is not None:
                query += " AND (d.decided_at, d.decision_id) < (?, ?)"
                parameters += list(before)
            query += " ORDER BY d.decided_at DESC, d.decision_id DESC LIMIT ?"
            rows = connection.execute(query, (*parameters, limit + 1)).fetchall()
            items = [{
                "decision_id": row["decision_id"], "lookup_id": row["lookup_id"],
                "hours": row["hours"], "time_text": row["time_text"],
                "source_work_version": row["source_work_version"],
                "resulting_work_version": row["resulting_work_version"],
                "lookup_retrieved_at": row["lookup_retrieved_at"],
                "decided_at": row["decided_at"], "decided_by": row["decided_by"],
                "decided_by_attribution": json.loads(row["decided_by_attribution_json"]),
                "redacted": row["result_json"] is None,
                "labels": _labels(json.loads(row["result_json"])) if row["result_json"] is not None else None,
            } for row in rows[:limit]]
            more = len(rows) > limit
            last = rows[limit - 1] if more else None
            return {"items": items,
                    "next": (last["decided_at"], last["decision_id"]) if last is not None else None}
