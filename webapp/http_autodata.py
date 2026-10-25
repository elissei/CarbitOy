"""Autodata 1B-3 HTTP boundary: five internal routes over the 1B-2 services.

An internal interface, not part of the v1 OpenAPI contract; leaving it out of
the schema is no security boundary (ROUTE_PERMISSIONS and the access gate
are). Every client check runs before a service call. Afterwards errors are
mapped by class only: of the ValueError classes only UnpriceableLabourTimeError
becomes 422, while an invariant ValueError is a server error, never a client
422. This module's own responses carry {"detail", "code"}, no-store and
nosniff; the gate's and _business_context's responses keep their own shape.
"""

import base64
import binascii
import json
import logging
import re
import sqlite3
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from webapp.auth_service import AuthenticationError, PermissionDeniedError
from webapp.autodata_records import AcceptLookupCommand, LookupCommand
from webapp.autodata_repository import (
    PAGE_LIMIT, AlreadyAcceptedError, AutodataClosedError, AutodataDisabledError, AutodataIntegrityError,
    ExpiredLookupError, IdentifierInRequestError, IdentifierInSearchTextError, InvalidLookupTimeError,
    LookupNotFoundError, LookupRedactedError, NotAcceptableError, RequestRedactedError, StaleLookupError,
    StaleViewError, SyntheticNotAcceptableError,
)
from webapp.autodata_service import (
    AutodataAcceptanceService, AutodataLookupService, LookupBusyError, LookupRateLimitedError,
)
from webapp.database import IdempotencyConflictError, WorkNotFoundError
from webapp.labour_snapshot import UnpriceableLabourTimeError
from webapp.workshop_context import WorkshopPermission


MAX_BODY_BYTES = 32 * 1024
MAX_CURSOR_CHARACTERS = 128
HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
_LIMIT = re.compile(r"[1-9][0-9]?")
_CURSOR = re.compile(r"[A-Za-z0-9_-]+")
_CURSOR_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00")

MESSAGES = {
    "NOT_FOUND": "Tiedustelua tai hakua ei löytynyt.",
    "PERMISSION_DENIED": "Ei oikeutta.",
    "AUTHENTICATION_REQUIRED": "Kirjautuminen vaaditaan.",
    "IDEMPOTENCY_CONFLICT": "Samaa pyyntöavainta on jo käytetty eri tiedoilla.",
    "STALE_VIEW": "Tiedustelu on muuttunut. Lataa näkymä uudelleen.",
    "STALE_LOOKUP": "Haku on tehty tiedustelun vanhalle versiolle. Tee uusi haku.",
    "EXPIRED": "Haku on vanhentunut. Tee uusi haku.",
    "INVALID_LOOKUP_TIME": "Haun aika on virheellinen. Tee uusi haku.",
    "AUTODATA_CLOSED": "Tiedustelun Autodata-aineisto on suljettu.",
    "NOT_ACCEPTABLE": "Hakutulosta ei voi hyväksyä.",
    "SYNTHETIC_NOT_ACCEPTABLE": "Synteettistä hakutulosta ei voi hyväksyä.",
    "ALREADY_ACCEPTED": "Haku on jo hyväksytty.",
    "REQUEST_REDACTED": "Hakupyynnön tiedot on poistettu. Tee uusi haku uudella pyynnöllä.",
    "LOOKUP_REDACTED": "Haun tiedot on poistettu, eikä sitä voi hyväksyä.",
    "LOOKUP_BUSY": "Toinen Autodata-haku on käynnissä. Yritä hetken kuluttua uudelleen.",
    "IDENTIFIER_IN_SEARCH_TEXT": "Hakuteksti ei saa sisältää rekisterinumeroa tai VIN-tunnusta.",
    "IDENTIFIER_IN_REQUEST": "Ajoneuvotiedot tai valinnat eivät saa sisältää rekisterinumeroa tai VIN-tunnusta.",
    "UNPRICEABLE_LABOUR": "Työajasta ei voi laskea hintaa.",
    "LOOKUP_RATE_LIMITED": "Autodata-hakujen tuntiraja on täynnä. Yritä myöhemmin uudelleen.",
    "AUTODATA_DISABLED": "Autodata-haku ei ole käytössä.",
    "AUTODATA_INTEGRITY": "Autodata-aineistossa on eheysvirhe.",
    "AUTODATA_UNAVAILABLE": "Autodata-palvelu ei ole käytettävissä.",
    "AUTODATA_ERROR": "Autodata-pyyntöä ei voitu käsitellä.",
    "INVALID_REQUEST": "Virheellinen Autodata-pyyntö.",
    "UNSUPPORTED_MEDIA_TYPE": "Pyynnön on oltava JSON-muotoinen.",
    "REQUEST_TOO_LARGE": "Pyyntö on liian suuri.",
    "INVALID_FRAMING": "Virheellinen pyynnön kehys.",
}
_CONFLICTS = (
    StaleViewError, StaleLookupError, ExpiredLookupError, InvalidLookupTimeError, AutodataClosedError,
    NotAcceptableError, SyntheticNotAcceptableError, AlreadyAcceptedError, RequestRedactedError,
    LookupRedactedError, LookupBusyError,
)

logger = logging.getLogger(__name__)


class AutodataHttpError(Exception):
    def __init__(self, status: int, code: str, headers: dict | None = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.headers = headers or {}


def _error_response(error: AutodataHttpError) -> JSONResponse:
    return JSONResponse({"detail": MESSAGES[error.code], "code": error.code}, status_code=error.status,
                        headers={**HEADERS, **error.headers})


def _invalid():
    raise AutodataHttpError(422, "INVALID_REQUEST")


# -- request boundary ---------------------------------------------------------

def _ready(request):
    state = request.app.state
    if not (isinstance(getattr(state, "autodata_lookups", None), AutodataLookupService)
            and isinstance(getattr(state, "autodata_acceptance", None), AutodataAcceptanceService)):
        raise AutodataHttpError(503, "AUTODATA_UNAVAILABLE")


def _path_uuid(request, name) -> str:
    value = request.path_params.get(name)
    try:
        if type(value) is not str or len(value) != 36:
            raise ValueError(name)
        canonical = str(UUID(value))
    except ValueError:
        raise AutodataHttpError(404, "NOT_FOUND") from None
    if canonical != value.lower():
        raise AutodataHttpError(404, "NOT_FOUND")
    return canonical


def _no_query(request):
    if request.query_params.multi_items():
        _invalid()


def encode_cursor(position) -> str | None:
    if position is None:
        return None
    moment, row_id = position
    return base64.urlsafe_b64encode(f"{moment}|{row_id}".encode("ascii")).rstrip(b"=").decode("ascii")


def _decode_cursor(value: str) -> tuple[str, str]:
    if len(value) > MAX_CURSOR_CHARACTERS or not _CURSOR.fullmatch(value):
        _invalid()
    try:
        text = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("ascii")
        moment, row_id = text.split("|")
        canonical = str(UUID(row_id))
    except (binascii.Error, UnicodeError, ValueError):
        _invalid()
    if (not _CURSOR_TIME.fullmatch(moment) or canonical != row_id
            or encode_cursor((moment, row_id)) != value):
        _invalid()
    return moment, row_id


def _page(request) -> tuple[int, tuple[str, str] | None]:
    pairs = request.query_params.multi_items()
    names = [name for name, _ in pairs]
    if len(names) != len(set(names)) or not set(names) <= {"limit", "before"}:
        _invalid()
    values = dict(pairs)
    limit = PAGE_LIMIT
    if "limit" in values:
        if not _LIMIT.fullmatch(values["limit"]) or int(values["limit"]) > PAGE_LIMIT:
            _invalid()
        limit = int(values["limit"])
    before = _decode_cursor(values["before"]) if "before" in values else None
    return limit, before


async def _command(request, model):
    types = request.headers.getlist("content-type")
    if len(types) != 1 or types[0].lower() not in ("application/json", "application/json; charset=utf-8"):
        raise AutodataHttpError(415, "UNSUPPORTED_MEDIA_TYPE")
    # The gate already admitted at most one all-digit Content-Length on a write.
    lengths = request.headers.getlist("content-length")
    if lengths and int(lengths[0]) > MAX_BODY_BYTES:
        raise AutodataHttpError(413, "REQUEST_TOO_LARGE")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise AutodataHttpError(413, "REQUEST_TOO_LARGE")
        body.extend(chunk)
    if lengths and int(lengths[0]) != len(body):
        raise AutodataHttpError(400, "INVALID_FRAMING")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("non-finite number")

    try:
        decoded = json.loads(body.decode("utf-8"), object_pairs_hook=unique, parse_constant=reject_constant)
        return model.model_validate(decoded)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        # Never serialize Pydantic errors: they echo the staff member's input.
        _invalid()


# -- service call and error mapping -------------------------------------------

def _call(operation):
    """Map service errors by class; no client error is inferred from a ValueError."""
    try:
        return operation()
    except (WorkNotFoundError, LookupNotFoundError):
        raise AutodataHttpError(404, "NOT_FOUND") from None
    # PermissionDeniedError subclasses AuthenticationError, so it is mapped first.
    except (PermissionDeniedError, PermissionError):
        raise AutodataHttpError(403, "PERMISSION_DENIED") from None
    except AuthenticationError:
        raise AutodataHttpError(401, "AUTHENTICATION_REQUIRED") from None
    except IdempotencyConflictError:
        raise AutodataHttpError(409, "IDEMPOTENCY_CONFLICT") from None
    except _CONFLICTS as error:
        raise AutodataHttpError(409, error.code) from None
    except (IdentifierInSearchTextError, IdentifierInRequestError) as error:
        raise AutodataHttpError(422, error.code) from None
    except UnpriceableLabourTimeError:
        raise AutodataHttpError(422, "UNPRICEABLE_LABOUR") from None
    except LookupRateLimitedError as error:
        raise AutodataHttpError(429, error.code, {"Retry-After": str(error.retry_after)}) from None
    except AutodataDisabledError:
        raise AutodataHttpError(503, "AUTODATA_DISABLED") from None
    except AutodataIntegrityError:
        logger.error("autodata integrity error")
        raise AutodataHttpError(500, "AUTODATA_INTEGRITY") from None
    except sqlite3.DatabaseError:
        raise AutodataHttpError(503, "AUTODATA_UNAVAILABLE") from None
    except Exception as error:
        # The class name only: messages may echo inputs or source text.
        logger.error("autodata request failed: %s", type(error).__name__)
        raise AutodataHttpError(500, "AUTODATA_ERROR") from None


# -- response projections -----------------------------------------------------

class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LookupView(_Closed):
    lookup_id: str
    inquiry_id: str
    source_work_version: int
    requested_at: str
    retrieved_at: str
    expires_at: str
    outcome: str
    level: str | None
    candidate_count: int
    hours: str | None
    time_text: str | None
    rejection_code: str | None
    synthetic: bool
    adapter_name: str
    adapter_version: str
    redacted: bool
    raw_time_text: str | None
    result: dict | None
    requested_by_name: str | None
    requested_by_me: bool


class LookupResponse(_Closed):
    replayed: bool
    lookup: LookupView


class LookupSummary(_Closed):
    lookup_id: str
    outcome: str
    hours: str | None
    time_text: str | None
    raw_time_text: str | None
    retrieved_at: str
    expires_at: str
    source_work_version: int
    synthetic: bool
    decision_id: str | None
    redacted: bool
    labels: dict | None
    acceptability: str


class LookupList(_Closed):
    items: list[LookupSummary]
    next: str | None
    autodata_enabled: bool


class DecisionReceipt(_Closed):
    decision_id: str
    inquiry_id: str
    lookup_id: str
    resulting_work_version: int
    decided_at: str


class AcceptResponse(_Closed):
    decision: DecisionReceipt
    replayed: bool
    work: dict


class DecisionView(_Closed):
    decision_id: str
    lookup_id: str
    hours: str
    time_text: str | None
    source_work_version: int
    resulting_work_version: int
    lookup_retrieved_at: str
    decided_at: str
    redacted: bool
    labels: dict | None
    decided_by_name: str | None
    decided_by_me: bool


class DecisionList(_Closed):
    items: list[DecisionView]
    next: str | None


def display_names(request, context, actors) -> dict:
    """Current names of actors of this caller's auth store only (DRAFT-B model).

    Runs after the business read has closed, keeping the auth -> business
    order. A storage outage of the name lookup only leaves names out.
    """
    ids = {actor for store, actor in actors if store == context.auth_store_id and actor}
    if not ids:
        return {}
    try:
        return request.app.state.auth_service.display_names(context.auth_store_id, ids)
    except (sqlite3.DatabaseError, RuntimeError):
        return {}


def actor_view(context, names, store, actor, prefix) -> dict:
    """An actor of another auth store gets no name and is never "me", even with the same ID."""
    own_store = store is not None and store == context.auth_store_id
    return {f"{prefix}_name": names.get(actor) if own_store else None,
            f"{prefix}_me": own_store and actor == context.employee_id}


def _lookup_view(record, context, names) -> dict:
    internal = {"request_id", "requested_by", "auth_store_id", "requested_by_attribution"}
    view = {key: value for key, value in record.items() if key not in internal}
    return view | actor_view(context, names, record.get("auth_store_id"), record.get("requested_by"),
                             "requested_by")


def _decision_view(item, context, names) -> dict:
    internal = {"decided_by", "auth_store_id", "decided_by_attribution"}
    view = {key: value for key, value in item.items() if key not in internal}
    return view | actor_view(context, names, item.get("auth_store_id"), item.get("decided_by"), "decided_by")


def _respond(model, data, status=200) -> JSONResponse:
    try:
        content = model.model_validate(data).model_dump(mode="json")
    except (ValidationError, ValueError, TypeError):
        # A closed projection met an unexpected repository field: a server fault.
        logger.error("autodata response projection failed")
        raise AutodataHttpError(500, "AUTODATA_ERROR") from None
    return JSONResponse(content, status_code=status, headers=HEADERS)


# -- operations (run in the thread pool) ----------------------------------------

def _create_lookup(request, resolver, work_id, command):
    context = resolver(request, WorkshopPermission.SAVE_LABOUR_PLAN)
    service = request.app.state.autodata_lookups
    result = _call(lambda: service.lookup(context, inquiry_id=work_id, command=command))
    record = result["lookup"]
    names = display_names(request, context, [(record.get("auth_store_id"), record.get("requested_by"))])
    return _respond(LookupResponse, {"replayed": result["replayed"],
                                     "lookup": _lookup_view(record, context, names)},
                    200 if result["replayed"] else 201)


def _list_lookups(request, resolver, work_id, limit, before):
    context = resolver(request, WorkshopPermission.READ_LABOUR_PLAN)
    settings = request.app.state.autodata_settings
    repository = request.app.state.autodata_repository
    page = _call(lambda: repository.list_lookups(
        context, inquiry_id=work_id, limit=limit, before=before,
        allow_synthetic=settings.allow_synthetic_acceptance))
    return _respond(LookupList, {"items": page["items"], "next": encode_cursor(page["next"]),
                                 "autodata_enabled": settings.enabled})


def _read_lookup(request, resolver, work_id, lookup_id):
    context = resolver(request, WorkshopPermission.READ_LABOUR_PLAN)
    repository = request.app.state.autodata_repository
    record = _call(lambda: repository.get_lookup(context, inquiry_id=work_id, lookup_id=lookup_id))
    names = display_names(request, context, [(record.get("auth_store_id"), record.get("requested_by"))])
    return _respond(LookupView, _lookup_view(record, context, names))


def _accept(request, resolver, work_id, lookup_id, command):
    context = resolver(request, WorkshopPermission.SAVE_LABOUR_PLAN)
    service = request.app.state.autodata_acceptance
    result = _call(lambda: service.accept(context, inquiry_id=work_id, lookup_id=lookup_id, command=command))
    return _respond(AcceptResponse, result)


def _list_decisions(request, resolver, work_id, limit, before):
    context = resolver(request, WorkshopPermission.READ_LABOUR_PLAN)
    repository = request.app.state.autodata_repository
    page = _call(lambda: repository.list_decisions(context, inquiry_id=work_id, limit=limit, before=before))
    names = display_names(request, context, [(item.get("auth_store_id"), item.get("decided_by"))
                                             for item in page["items"]])
    return _respond(DecisionList, {"items": [_decision_view(item, context, names) for item in page["items"]],
                                   "next": encode_cursor(page["next"])})


def register_autodata_routes(app, context_resolver):
    def guarded(handler):
        async def endpoint(request: Request):
            try:
                return await handler(request)
            except AutodataHttpError as error:
                return _error_response(error)
        return endpoint

    async def create_lookup(request):
        _ready(request)
        _no_query(request)
        work_id = _path_uuid(request, "work_id")
        command = await _command(request, LookupCommand)
        return await run_in_threadpool(_create_lookup, request, context_resolver, work_id, command)

    async def list_lookups(request):
        _ready(request)
        limit, before = _page(request)
        work_id = _path_uuid(request, "work_id")
        return await run_in_threadpool(_list_lookups, request, context_resolver, work_id, limit, before)

    async def read_lookup(request):
        _ready(request)
        _no_query(request)
        work_id, lookup_id = _path_uuid(request, "work_id"), _path_uuid(request, "lookup_id")
        return await run_in_threadpool(_read_lookup, request, context_resolver, work_id, lookup_id)

    async def accept_lookup(request):
        _ready(request)
        _no_query(request)
        work_id, lookup_id = _path_uuid(request, "work_id"), _path_uuid(request, "lookup_id")
        command = await _command(request, AcceptLookupCommand)
        return await run_in_threadpool(_accept, request, context_resolver, work_id, lookup_id, command)

    async def list_decisions(request):
        _ready(request)
        limit, before = _page(request)
        work_id = _path_uuid(request, "work_id")
        return await run_in_threadpool(_list_decisions, request, context_resolver, work_id, limit, before)

    prefix = "/api/works/{work_id}/autodata"
    for path, method, handler in (
        (prefix + "/lookups", "POST", create_lookup),
        (prefix + "/lookups", "GET", list_lookups),
        (prefix + "/lookups/{lookup_id}", "GET", read_lookup),
        (prefix + "/lookups/{lookup_id}/accept", "POST", accept_lookup),
        (prefix + "/decisions", "GET", list_decisions),
    ):
        # Internal interface, not part of the v1 OpenAPI contract; not a security boundary.
        app.add_api_route(path, guarded(handler), methods=[method], include_in_schema=False)
