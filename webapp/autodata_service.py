"""Autodata 1B-2 services: one serialized lookup and a thin acceptance delegate.

Disabled (MAIN decision 24.10.2026, replaces decision 4): no new adapter
lookup and no new acceptance, but a stored lookup and a successful
acceptance are still replayed under the current permissions, workshop scope
and conflict checks. The acceptance receipt check and the refusal of a new
write happen in the same repository transaction. Not wired into the HTTP
application (1B-3). This phase supports only the explicitly injected fake
adapter, bound to its own class and run in-process (MAIN decision 2).

Lookup order (plan 3.2): stored retry -> process lock -> stored retry again ->
prepare -> server-completed request -> adapter outside every operation block
-> store -> lock released in finally. The repository revalidates
authorization and workshop scope in every operation, so a membership revoked
or an inquiry closed while the adapter ran stores nothing.
"""

from integrations.autodata.fake_adapter import FAKE_ADAPTER_VERSION, FakeAutodataLabourAdapter
from integrations.autodata.runner import PROCESS_LOOKUP_LOCK, LookupRunner
from webapp.autodata_migrations import ADAPTERS
from webapp.autodata_records import AcceptLookupCommand, LookupCommand, snapshot_has_identifier, split_request
from webapp.autodata_repository import (
    AutodataDisabledError, AutodataError, AutodataRepository, IdentifierInRequestError,
)
from webapp.autodata_settings import AutodataSettings
from webapp.workshop_context import TrustedWorkshopContext, WorkshopPermission


# The fake only: the synthetic page reader drives a browser in-process (plan 3.1).
SUPPORTED_ADAPTER = ("fake", FAKE_ADAPTER_VERSION, True)
# The declared identity alone is not enough: the adapter must be this exact class.
SUPPORTED_ADAPTER_CLASS = FakeAutodataLabourAdapter


class LookupBusyError(AutodataError):
    """Another lookup is running in this process; concurrency guard, not a rate limit."""

    code = "LOOKUP_BUSY"


def _authorize(context, permission) -> None:
    if not isinstance(context, TrustedWorkshopContext) or permission not in context.permissions:
        raise PermissionError("a trusted authorized workshop context is required")


def _settings(settings) -> AutodataSettings:
    if not isinstance(settings, AutodataSettings):
        raise TypeError("server autodata settings are required")
    return settings


def _repository(repository) -> AutodataRepository:
    if not isinstance(repository, AutodataRepository):
        raise TypeError("an autodata repository is required")
    return repository


def _supported_runner(runner) -> LookupRunner:
    """The plain runner with the fake's identity AND the fake's own class; checked before any call."""
    if type(runner) is not LookupRunner:
        raise TypeError("an enabled autodata service requires a lookup runner")
    identity = runner.identity
    key = (identity.name, identity.version, identity.synthetic)
    if (key != SUPPORTED_ADAPTER or key[:2] not in ADAPTERS
            or type(runner.adapter) is not SUPPORTED_ADAPTER_CLASS):
        raise ValueError("this phase supports only the injected fake adapter")
    return runner


class AutodataLookupService:
    def __init__(self, repository: AutodataRepository, settings: AutodataSettings,
                 runner: LookupRunner | None = None):
        self.repository = _repository(repository)
        self.settings = _settings(settings)
        self.runner = _supported_runner(runner) if settings.enabled else runner

    def lookup(self, context, *, inquiry_id: str, command: LookupCommand) -> dict:
        _authorize(context, WorkshopPermission.SAVE_LABOUR_PLAN)
        command = LookupCommand.model_validate(command.model_dump())
        command_sha256 = command.command_sha256(actor_user_id=context.employee_id, inquiry_id=inquiry_id)
        # A read-only retry check: current permission, scope and conflict; no writes.
        stored = self.repository.find_stored_lookup(
            context, inquiry_id=inquiry_id, request_id=command.request_id, command_sha256=command_sha256)
        if stored is not None:
            return {"lookup": stored, "replayed": True}
        if not self.settings.enabled:
            raise AutodataDisabledError()
        # Rechecked here: a runner rewired after construction never reaches the adapter.
        runner = _supported_runner(self.runner)
        if not PROCESS_LOOKUP_LOCK.acquire(blocking=False):
            raise LookupBusyError()
        try:
            # A parallel request may have been stored before this lock was taken.
            stored = self.repository.find_stored_lookup(
                context, inquiry_id=inquiry_id, request_id=command.request_id,
                command_sha256=command_sha256)
            if stored is not None:
                return {"lookup": stored, "replayed": True}
            preparation = self.repository.prepare_lookup(
                context, inquiry_id=inquiry_id, expected_version=command.expected_version,
                search_text=command.work_query, vin=command.vehicle_facts.vin)
            request = command.adapter_request(registration=preparation.registration)
            # store_lookup would reject it after the adapter call; never call the adapter for it.
            snapshot, inputs = split_request(request)
            if snapshot_has_identifier(snapshot, registration=preparation.registration, vin=inputs["vin"]):
                raise IdentifierInRequestError(inquiry_id)
            run = runner.run(request)
            record = self.repository.store_lookup(
                context, inquiry_id=inquiry_id, request_id=command.request_id,
                command_sha256=command_sha256, request=request, result=run.result,
                source_work_version=preparation.source_work_version, requested_at=run.requested_at,
                # The stored column always equals the result's own provenance time.
                retrieved_at=run.result.provenance.retrieved_at,
            )
            return {"lookup": record, "replayed": False}
        finally:
            PROCESS_LOOKUP_LOCK.release()


class AutodataAcceptanceService:
    """Delegates to the one repository acceptance path and its receipt contract."""

    def __init__(self, repository: AutodataRepository, settings: AutodataSettings):
        self.repository = _repository(repository)
        self.settings = _settings(settings)

    def accept(self, context, *, inquiry_id: str, lookup_id: str, command: AcceptLookupCommand) -> dict:
        _authorize(context, WorkshopPermission.SAVE_LABOUR_PLAN)
        if not self.settings.enabled:
            # Receipt replay only; the repository refuses a new write in the same transaction.
            return self.repository.accept_lookup(
                context, inquiry_id=inquiry_id, lookup_id=lookup_id, command=command, replay_only=True)
        return self.repository.accept_lookup(
            context, inquiry_id=inquiry_id, lookup_id=lookup_id, command=command,
            allow_synthetic=self.settings.allow_synthetic_acceptance)
