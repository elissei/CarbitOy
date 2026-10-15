"""Internal Web 4B service; deliberately not exposed by HTTP routes.

The caller must obtain TrustedWorkshopContext from trusted server code. This
contract is not authentication and must never be constructed from browser data.
Domain planning/review logic is the approved implementation from 06a0ee4.
Operation responses are historical retry snapshots, not proof of current
approval; consumers must use require_approved_final_hours on current storage.
"""

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from uuid import uuid4

from pydantic import Field, model_validator

from domain.models.labour_plan import LabourPlan, LabourReviewDecision, LabourReviewStatus, LabourTimeEntry
from domain.models.labour_time import LabourTime
from domain.models.work_plan import WorkPlan
from services.labour_planning import approve_labour_plan_adjusted, approve_labour_plan_unchanged
from webapp.database import IdempotencyConflictError, VersionConflictError, WorkNotFoundError, WorkRepository
from webapp.business_identity import BusinessActor
from webapp.workshop_context import TrustedWorkshopContext, WorkshopPermission
from webapp.labour_models import (
    CoverageStatus, LabourPlanRequestRecord, LabourPlanRevisionRecord,
    LabourReviewDecisionRecord, OriginalWorkTimeRecord, ReviewDecisionStatus,
    StorageRecord, decimal_storage_text,
)


class StaleLabourPlanError(ValueError):
    pass


class StoredLabourPlanError(ValueError):
    pass


class LabourPlanCommand(StorageRecord):
    inquiry_id: str = Field(min_length=1)
    expected_version: int = Field(ge=1, strict=True)
    expected_content_id: str = Field(pattern="^[0-9a-f]{64}$")
    expected_rules_version: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)


class SaveLabourPlanCommand(LabourPlanCommand):
    work_plan: WorkPlan
    original_times: tuple[OriginalWorkTimeRecord, ...] = Field(min_length=1)


class ApproveLabourPlanCommand(LabourPlanCommand):
    expected_revision_id: str = Field(min_length=1)
    status: ReviewDecisionStatus
    final_billable_hours: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    reason: str | None = None

    @model_validator(mode="after")
    def validate_review_request(self):
        if self.status == ReviewDecisionStatus.APPROVED_ADJUSTED:
            if self.final_billable_hours is None or self.reason is None:
                raise ValueError("adjusted approval requires final hours and a reason")
        elif self.final_billable_hours is not None or self.reason is not None:
            raise ValueError("unchanged approval cannot specify an adjustment")
        return self


class LabourOperationResult(StorageRecord):
    inquiry_id: str
    revision_id: str
    revision: int = Field(ge=1, strict=True)
    work_version: int = Field(ge=1, strict=True)
    content_id: str
    rules_version: str
    coverage_status: CoverageStatus
    original_total_hours: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    human_review_required: bool
    review_status: LabourReviewStatus = LabourReviewStatus.PENDING
    decision_id: str | None = None
    final_billable_hours: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)


@dataclass(frozen=True)
class StoredLabourPlan:
    revision: LabourPlanRevisionRecord
    work_version: int
    plan: LabourPlan
    decision_id: str | None


@contextmanager
def _exact_original_arithmetic(originals):
    """Preserve original sums without changing the domain's arithmetic rules."""
    hours = [item.original_hours for item in originals if item.original_hours is not None]
    with localcontext() as context:
        if hours:
            context.prec = max(
                context.prec, max(value.adjusted() for value in hours)
                - min(value.as_tuple().exponent for value in hours)
                + len(str(len(hours))) + 1,
            )
        yield


def build_storage_labour_plan(work_plan: WorkPlan, originals) -> LabourPlan:
    """One full-scope original record per work; never infer combined times.

    Missing time is represented explicitly by original_hours=None. A BOTH work
    must carry its correct pair time. Separate opposite-side works remain under
    the domain's missing-pair coverage guard rather than summing a guessed pair.
    """
    works = WorkPlan.model_validate(work_plan.model_dump(round_trip=True))
    items = tuple(OriginalWorkTimeRecord.model_validate(item.model_dump()) for item in originals)
    work_by_id = {work.work_id: work.work_item for work in works.work_items}
    if len(items) != len(work_by_id) or {item.work_item_id for item in items} != set(work_by_id):
        raise ValueError("exactly one original record is required for each work")
    times = []
    for item in sorted(items, key=lambda item: item.work_item_id):
        work = work_by_id[item.work_item_id]
        if (item.operation_identifier, item.axle, item.side) != (
            work.operation.value, work.axle.value, work.side.value,
        ):
            raise ValueError("original operation and scope must match its work")
        if item.original_hours is not None:
            times.append(LabourTimeEntry(
                time_id=f"original:{item.work_item_id}", covered_work_ids=[item.work_item_id],
                side=work.side, labour_time=LabourTime(
                    hours=item.original_hours, source=item.source,
                    work_description=item.work_description, recorded_by=item.recorded_by,
                    autodata_reference=item.autodata_reference,
                ),
            ))
    return LabourPlan(work_plan=works, original_times=times,
                      selected_time_ids=[entry.time_id for entry in times])


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class LabourPlanService:
    def __init__(self, repository: WorkRepository):
        self.repository = repository

    @staticmethod
    def _authorize(context, permission):
        if not isinstance(context, TrustedWorkshopContext) or permission not in context.permissions:
            raise PermissionError("a trusted authorized workshop context is required")

    @staticmethod
    def _inquiry(connection, inquiry_id, expected_version=None, *, context):
        row = WorkRepository._find_inquiry(connection, inquiry_id, context)
        if expected_version is not None and row["version"] != expected_version:
            raise VersionConflictError(inquiry_id)
        return row

    @staticmethod
    def _check_identity(plan, content_id, rules_version):
        if rules_version != plan.review_rules_version or content_id != plan.content_id:
            raise StaleLabourPlanError("plan content or review rules changed")

    @staticmethod
    def _request_json(command, context, operation):
        content = command.model_dump(mode="json", round_trip=True)
        if context.actor is not None:
            return _json({"contract": "carbit.workshop.authenticated.v1", "operation": operation,
                          "actor_user_id": context.employee_id, "command": content})
        return _json(content)

    @staticmethod
    def _replay(connection, command, context, operation, request_json):
        if context.actor is not None:
            # The existing inquiry/operation key scope is retained. A new auth
            # caller cannot adopt another actor's or an unversioned legacy retry.
            rows = connection.execute("""
                SELECT actor_id, request_json, response_json FROM labour_plan_requests
                WHERE inquiry_id=? AND operation=? AND idempotency_key=?
            """, (command.inquiry_id, operation, command.idempotency_key)).fetchall()
            if not rows:
                return None
            if len(rows) != 1 or rows[0]["actor_id"] != context.employee_id or rows[0]["request_json"] != request_json:
                raise IdempotencyConflictError(command.idempotency_key)
            return LabourOperationResult.model_validate_json(rows[0]["response_json"])
        previous = connection.execute("""
            SELECT request_json, response_json FROM labour_plan_requests
            WHERE inquiry_id=? AND actor_id=? AND operation=? AND idempotency_key=?
        """, (command.inquiry_id, context.employee_id, operation, command.idempotency_key)).fetchone()
        if previous is None:
            return None
        if previous["request_json"] != request_json:
            raise IdempotencyConflictError(command.idempotency_key)
        return LabourOperationResult.model_validate_json(previous["response_json"])

    @staticmethod
    def _bump_version(connection, inquiry_id, expected_version, timestamp):
        cursor = connection.execute("""
            UPDATE work_inquiries SET version=version+1, updated_at=?
            WHERE id=? AND version=?
        """, (timestamp.isoformat(), inquiry_id, expected_version))
        if cursor.rowcount != 1:
            raise VersionConflictError(inquiry_id)
        return expected_version + 1

    @staticmethod
    def _append_event(connection, inquiry_id, timestamp, event_type, context, details, version):
        if context.actor is not None:
            details = {**details, "actor_attribution": context.actor.attribution()}
        return connection.execute("""
            INSERT INTO work_events (work_id, occurred_at, event_type, actor, details_json, work_version)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (inquiry_id, timestamp.isoformat(), event_type, context.employee_id,
              _json(details), version)).lastrowid

    @staticmethod
    def _remember(connection, command, context, operation, request_json, result, timestamp):
        record = LabourPlanRequestRecord(
            inquiry_id=command.inquiry_id, actor_id=context.employee_id, operation=operation,
            idempotency_key=command.idempotency_key, request_json=request_json,
            response_json=result.model_dump_json(), revision_id=result.revision_id,
            decision_id=result.decision_id, result_work_version=result.work_version, created_at=timestamp,
        )
        connection.execute("""
            INSERT INTO labour_plan_requests (
                inquiry_id, actor_id, operation, idempotency_key, request_json,
                response_json, revision_id, decision_id, result_work_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (record.inquiry_id, record.actor_id, record.operation, record.idempotency_key,
              record.request_json, record.response_json, record.revision_id, record.decision_id,
              record.result_work_version, record.created_at.isoformat()))

    @staticmethod
    def _result(record, version, plan, decision_id=None):
        return LabourOperationResult(
            inquiry_id=record.inquiry_id, revision_id=record.revision_id, revision=record.revision,
            work_version=version, content_id=record.content_id, rules_version=record.rules_version,
            coverage_status=record.coverage_status, original_total_hours=record.original_total_hours,
            human_review_required=bool(plan.review_flags), review_status=plan.review_status,
            decision_id=decision_id, final_billable_hours=plan.approved_final_hours,
        )

    @staticmethod
    def _insert_revision(connection, record, plan):
        for position, item in enumerate(record.items, 1):
            connection.execute("""
                INSERT INTO inquiry_work_items (inquiry_id, work_item_id, created_at) VALUES (?, ?, ?)
                ON CONFLICT (inquiry_id, work_item_id) DO NOTHING
            """, (record.inquiry_id, item.work_item_id, record.created_at.isoformat()))
            connection.execute("""
                INSERT INTO labour_plan_revision_items (
                    revision_id, inquiry_id, work_item_id, position, operation_identifier,
                    axle, side, original_hours, source, work_description, autodata_reference, recorded_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (record.revision_id, record.inquiry_id, item.work_item_id, position,
                  item.operation_identifier, item.axle, item.side,
                  decimal_storage_text(item.original_hours) if item.original_hours is not None else None,
                  item.source.value if item.source is not None else None,
                  item.work_description, item.autodata_reference, item.recorded_by))
        connection.execute("""
            INSERT INTO labour_plan_revisions (
                revision_id, inquiry_id, revision, content_id, rules_version, coverage_status,
                original_total_hours, work_item_count, snapshot_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (record.revision_id, record.inquiry_id, record.revision, record.content_id,
              record.rules_version, record.coverage_status.value,
              decimal_storage_text(record.original_total_hours) if record.original_total_hours is not None else None,
              len(record.items), _json({"format": "web4b-v1", "plan": plan.model_dump(mode="json", round_trip=True)}),
              record.created_at.isoformat()))

    def save_plan(self, context: TrustedWorkshopContext, command: SaveLabourPlanCommand) -> LabourOperationResult:
        self._authorize(context, WorkshopPermission.SAVE_LABOUR_PLAN)
        command = SaveLabourPlanCommand.model_validate(command.model_dump(round_trip=True))
        request_json = self._request_json(command, context, "SAVE_PLAN")
        with self.repository.operation(context, WorkshopPermission.SAVE_LABOUR_PLAN, write=True) as connection:
            self._inquiry(connection, command.inquiry_id, context=context)
            replay = self._replay(connection, command, context, "SAVE_PLAN", request_json)
            if replay is not None:
                return replay
            inquiry = self._inquiry(connection, command.inquiry_id, command.expected_version, context=context)
            with _exact_original_arithmetic(command.original_times):
                plan = build_storage_labour_plan(command.work_plan, command.original_times)
                self._check_identity(plan, command.expected_content_id, command.expected_rules_version)
                if plan.work_plan.vehicle.registration != inquiry["registration"]:
                    raise ValueError("vehicle registration must match the inquiry")
                number = connection.execute(
                    "SELECT coalesce(max(revision), 0)+1 FROM labour_plan_revisions WHERE inquiry_id=?",
                    (command.inquiry_id,),
                ).fetchone()[0]
                timestamp = datetime.now(timezone.utc)
                record = LabourPlanRevisionRecord(
                    revision_id=str(uuid4()), inquiry_id=command.inquiry_id, revision=number,
                    content_id=plan.content_id, rules_version=plan.review_rules_version,
                    coverage_status=plan.status.value, original_total_hours=plan.total_hours,
                    items=tuple(sorted(command.original_times, key=lambda item: item.work_item_id)),
                    created_at=timestamp,
                )
                self._insert_revision(connection, record, plan)
                connection.execute("""
                    INSERT INTO inquiry_labour_plan_state (inquiry_id, active_revision_id, active_decision_id)
                    VALUES (?, ?, NULL)
                    ON CONFLICT (inquiry_id) DO UPDATE
                    SET active_revision_id=excluded.active_revision_id, active_decision_id=NULL
                """, (record.inquiry_id, record.revision_id))
                version = self._bump_version(connection, record.inquiry_id, command.expected_version, timestamp)
                self._append_event(connection, record.inquiry_id, timestamp, "LABOUR_PLAN_SAVED",
                                   context, record.model_dump(mode="json"), version)
                result = self._result(record, version, plan)
                self._remember(connection, command, context, "SAVE_PLAN", request_json, result, timestamp)
                return result

    @staticmethod
    def _history(connection, inquiry_id):
        return tuple(LabourReviewDecisionRecord.model_validate(dict(row)) for row in connection.execute("""
            SELECT decision_id, inquiry_id, revision_id, content_id, rules_version, status,
                   original_total_hours, final_billable_hours, reason, reviewer_id, reviewed_at, event_id
            FROM labour_review_decisions WHERE inquiry_id=? ORDER BY event_id
        """, (inquiry_id,)))

    @staticmethod
    def _domain_decision(record):
        return LabourReviewDecision(
            plan_content_id=record.content_id, rules_version=record.rules_version,
            status=record.status.value, original_total_hours=record.original_total_hours,
            final_hours=(record.final_billable_hours if record.status == ReviewDecisionStatus.APPROVED_ADJUSTED else None),
            reason=record.reason, reviewer_id=record.reviewer_id, reviewed_at=record.reviewed_at,
        )

    def _load_active(self, connection, inquiry):
        row = connection.execute("""
            SELECT r.*, s.active_decision_id FROM inquiry_labour_plan_state s
            JOIN labour_plan_revisions r ON r.revision_id=s.active_revision_id AND r.inquiry_id=s.inquiry_id
            WHERE s.inquiry_id=?
        """, (inquiry["id"],)).fetchone()
        if row is None:
            raise ValueError("inquiry has no active labour plan")
        latest = connection.execute("SELECT max(revision) FROM labour_plan_revisions WHERE inquiry_id=?", (inquiry["id"],)).fetchone()[0]
        if row["revision"] != latest:
            raise StaleLabourPlanError("active revision is no longer the latest revision")
        items = tuple(OriginalWorkTimeRecord(
            work_item_id=item["work_item_id"], operation_identifier=item["operation_identifier"],
            axle=item["axle"], side=item["side"], original_hours=item["original_hours"],
            source=item["source"], work_description=item["work_description"],
            autodata_reference=item["autodata_reference"], recorded_by=item["recorded_by"],
        ) for item in connection.execute("SELECT * FROM labour_plan_revision_items WHERE revision_id=? ORDER BY position", (row["revision_id"],)))
        record = LabourPlanRevisionRecord(
            revision_id=row["revision_id"], inquiry_id=row["inquiry_id"], revision=row["revision"],
            content_id=row["content_id"], rules_version=row["rules_version"], coverage_status=row["coverage_status"],
            original_total_hours=row["original_total_hours"], items=items, created_at=row["created_at"],
        )
        snapshot = json.loads(row["snapshot_json"])
        if snapshot.get("format") != "web4b-v1":
            raise StoredLabourPlanError("unsupported plan snapshot; explicit new revision is required")
        source = LabourPlan.model_validate(snapshot["plan"])
        if source.review_decision is not None or source.review_history:
            raise StoredLabourPlanError("source revision must not contain approval decisions")
        with _exact_original_arithmetic(items):
            plan = build_storage_labour_plan(source.work_plan, items)
            if plan.work_plan.vehicle.registration != inquiry["registration"]:
                raise StoredLabourPlanError("stored vehicle does not match inquiry")
            if source.content_id != plan.content_id or row["work_item_count"] != len(items):
                raise StoredLabourPlanError("source snapshot and original rows disagree")
            self._check_identity(plan, record.content_id, record.rules_version)
            if record.original_total_hours != plan.total_hours or record.coverage_status.value != plan.status.value:
                raise StoredLabourPlanError("stored sum or coverage disagrees with domain plan")
        history = self._history(connection, inquiry["id"])
        active_id = row["active_decision_id"]
        if active_id is not None:
            if not history or history[-1].decision_id != active_id:
                raise StaleLabourPlanError("active decision is not the latest decision")
            plan.review_decision = self._domain_decision(history[-1])
            history = history[:-1]
        plan.review_history = tuple(self._domain_decision(item) for item in history)
        return StoredLabourPlan(record, inquiry["version"], plan, active_id)

    def approve_plan(self, context: TrustedWorkshopContext, command: ApproveLabourPlanCommand) -> LabourOperationResult:
        self._authorize(context, WorkshopPermission.APPROVE_LABOUR_PLAN)
        command = ApproveLabourPlanCommand.model_validate(command.model_dump(round_trip=True))
        request_json = self._request_json(command, context, "APPROVE_LABOUR")
        with self.repository.operation(context, WorkshopPermission.APPROVE_LABOUR_PLAN, write=True) as connection:
            self._inquiry(connection, command.inquiry_id, context=context)
            replay = self._replay(connection, command, context, "APPROVE_LABOUR", request_json)
            if replay is not None:
                return replay
            inquiry = self._inquiry(connection, command.inquiry_id, command.expected_version, context=context)
            stored = self._load_active(connection, inquiry)
            if command.expected_revision_id != stored.revision.revision_id:
                raise StaleLabourPlanError("active revision changed since review")
            with _exact_original_arithmetic(stored.revision.items):
                self._check_identity(stored.plan, command.expected_content_id, command.expected_rules_version)
                if command.status == ReviewDecisionStatus.APPROVED_UNCHANGED:
                    plan = approve_labour_plan_unchanged(stored.plan, command.expected_content_id, context.employee_id)
                else:
                    plan = approve_labour_plan_adjusted(stored.plan, command.expected_content_id, context.employee_id,
                                                        command.final_billable_hours, command.reason)
                decision = plan.review_decision
                timestamp = decision.reviewed_at
                version = self._bump_version(connection, command.inquiry_id, command.expected_version, timestamp)
                decision_id = str(uuid4())
                details = dict(
                    decision_id=decision_id, inquiry_id=command.inquiry_id, revision_id=stored.revision.revision_id,
                    content_id=decision.plan_content_id, rules_version=decision.rules_version, status=decision.status.value,
                    original_total_hours=decimal_storage_text(stored.revision.original_total_hours),
                    final_billable_hours=decimal_storage_text(plan.require_approved_final_hours()),
                    reason=decision.reason, reviewer_id=context.employee_id, reviewed_at=timestamp.isoformat(),
                )
                event_id = self._append_event(connection, command.inquiry_id, timestamp, "LABOUR_REVIEW_DECIDED",
                                              context, details, version)
                record = LabourReviewDecisionRecord(**details, event_id=event_id)
                connection.execute("""
                    INSERT INTO labour_review_decisions (
                        decision_id, inquiry_id, revision_id, content_id, rules_version, status,
                        original_total_hours, final_billable_hours, reason, reviewer_id, reviewed_at, event_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (record.decision_id, record.inquiry_id, record.revision_id, record.content_id,
                      record.rules_version, record.status.value, decimal_storage_text(record.original_total_hours),
                      decimal_storage_text(record.final_billable_hours), record.reason, record.reviewer_id,
                      record.reviewed_at.isoformat(), record.event_id))
                cursor = connection.execute("""
                    UPDATE inquiry_labour_plan_state SET active_decision_id=?
                    WHERE inquiry_id=? AND active_revision_id=?
                """, (record.decision_id, record.inquiry_id, record.revision_id))
                if cursor.rowcount != 1:
                    raise StaleLabourPlanError("active plan changed")
                result = self._result(stored.revision, version, plan, decision_id)
                self._remember(connection, command, context, "APPROVE_LABOUR", request_json, result, timestamp)
                return result

    def get_active_plan(self, context: TrustedWorkshopContext, inquiry_id: str) -> StoredLabourPlan:
        self._authorize(context, WorkshopPermission.READ_LABOUR_PLAN)
        with self.repository.operation(context, WorkshopPermission.READ_LABOUR_PLAN, write=False) as connection:
            return self._load_active(connection, self._inquiry(connection, inquiry_id, context=context))

    def decision_history(self, context: TrustedWorkshopContext, inquiry_id: str):
        self._authorize(context, WorkshopPermission.READ_LABOUR_PLAN)
        with self.repository.operation(context, WorkshopPermission.READ_LABOUR_PLAN, write=False) as connection:
            self._inquiry(connection, inquiry_id, context=context)
            return self._history(connection, inquiry_id)

    def require_approved_final_hours(self, context: TrustedWorkshopContext, *, inquiry_id: str,
                                     expected_version: int, expected_content_id: str,
                                     expected_rules_version: str, expected_revision_id: str) -> Decimal:
        """Current approved Decimal only; no pricing, quote creation or sending."""
        self._authorize(context, WorkshopPermission.READ_LABOUR_PLAN)
        with self.repository.operation(context, WorkshopPermission.READ_LABOUR_PLAN, write=False) as connection:
            inquiry = self._inquiry(connection, inquiry_id, expected_version, context=context)
            stored = self._load_active(connection, inquiry)
            if expected_revision_id != stored.revision.revision_id:
                raise StaleLabourPlanError("active revision changed")
            with _exact_original_arithmetic(stored.revision.items):
                self._check_identity(stored.plan, expected_content_id, expected_rules_version)
                return stored.plan.require_approved_final_hours()
