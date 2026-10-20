"""Bounded JSON transport over DraftService, not another domain repository."""

import json
import re
import sqlite3
from typing import Annotated, Literal
from uuid import UUID

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictStr, TypeAdapter, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from webapp.auth_service import AuthenticationError, PermissionDeniedError
from webapp.draft_keys import DraftKeyConfigurationError
from webapp.draft_models import (
    DraftNotFoundError, DraftRevisionConflictError, DraftIdempotencyConflictError,
)
from webapp.draft_service import DraftService
from webapp.workshop_context import WorkshopAccessDenied, WorkshopPermission


MAX_BODY_BYTES = 64 * 1024
Revision = Annotated[int,Field(strict=True,ge=1,le=9223372036854775807)]
EditorPayload = dict[str,StrictStr | None]


def canonical_uuid(value):
    if type(value) is not str or len(value)!=36:
        raise ValueError('UUID required')
    normalized = str(UUID(value))
    if normalized!=value.lower():
        raise ValueError('UUID required')
    return normalized


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: StrictStr

    @field_validator('request_id')
    @classmethod
    def uuid_request(cls,value):
        return canonical_uuid(value)


class CreateInquiryDraft(StrictRequest):
    draft_type: Literal['WORK_INQUIRY']
    payload: EditorPayload


class CreateLabourDraft(StrictRequest):
    draft_type: Literal['LABOUR_ENTRY']
    work_id: Annotated[StrictStr,Field(min_length=1,max_length=200)]
    original_work_version: Revision
    payload: EditorPayload


CREATE_REQUEST = TypeAdapter(Annotated[CreateInquiryDraft | CreateLabourDraft,Field(discriminator='draft_type')])


class SaveDraft(StrictRequest):
    expected_revision: Revision
    payload: EditorPayload


class DiscardDraft(StrictRequest):
    expected_revision: Revision


class ReceiptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True,extra='forbid')
    accepted: Literal[True] = True
    draft_id: str
    draft_type: Literal['WORK_INQUIRY','LABOUR_ENTRY']
    revision: int
    state: Literal['ACTIVE','DISCARDED']
    last_content_saved_at: int
    expires_at: int
    termination_reason: Literal['DISCARDED','EXPIRED','CONVERTED'] | None
    terminated_at: int | None


class SnapshotResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True,extra='forbid')
    draft_id: str
    draft_type: Literal['WORK_INQUIRY','LABOUR_ENTRY']
    revision: int
    state: Literal['ACTIVE'] = 'ACTIVE'
    payload: EditorPayload
    payload_schema_version: Literal[1]
    work_id: str | None
    original_work_version: int | None
    created_at: int
    last_content_saved_at: int
    expires_at: int
    # Current display names at read time; actor IDs never reach the browser.
    created_by_name: str | None = None
    created_by_me: bool = False
    last_editor_known: bool = False
    last_edited_by_name: str | None = None
    last_edited_by_me: bool = False


def _attribution(request, context, snapshots):
    """Names for actors of drafts the caller was already authorized to read.

    Runs after the business read and its auth binding have closed, so the
    auth -> business order is kept. Only a storage outage of the name lookup
    degrades to missing names; the draft read itself has already succeeded.
    """
    ids={value for row in snapshots for value in (row.creator_id,row.last_editor_id) if value}
    stores={row.actor_store_id for row in snapshots}
    names={}
    if ids and len(stores)==1:
        try:
            names=request.app.state.auth_service.display_names(next(iter(stores)),ids)
        except (sqlite3.DatabaseError,RuntimeError):
            names={}
    def mine(row,actor):
        return actor is not None and row.actor_store_id==context.auth_store_id and actor==context.employee_id
    return [{'created_by_name':names.get(row.creator_id),'created_by_me':mine(row,row.creator_id),
             'last_editor_known':row.last_editor_id is not None,'last_edited_by_name':names.get(row.last_editor_id),
             'last_edited_by_me':mine(row,row.last_editor_id)} for row in snapshots]


def _error(status,detail):
    raise HTTPException(status_code=status,detail=detail,headers={'Cache-Control':'no-store'})


def _ready(request):
    service=getattr(request.app.state,'draft_service',None)
    if not isinstance(service,DraftService):
        _error(503,'Draft service unavailable')
    return service


def _query(request, *, listing=False):
    pairs=list(request.query_params.multi_items())
    if not pairs:
        return 50
    if (not listing or len(pairs)!=1 or pairs[0][0]!='limit'
            or not re.fullmatch(r'[1-9][0-9]{0,2}',pairs[0][1])
            or not 1<=int(pairs[0][1])<=100):
        _error(422,'Invalid draft request')
    return int(pairs[0][1])


def _draft_id(request):
    try:
        return canonical_uuid(request.path_params['draft_id'])
    except (ValueError,TypeError,KeyError):
        _error(404,'Draft unavailable')


async def _payload(request, schema):
    types=request.headers.getlist('content-type')
    if len(types)!=1 or types[0].lower() not in ('application/json','application/json; charset=utf-8'):
        _error(415,'JSON draft request required')
    lengths=request.headers.getlist('content-length')
    if lengths and int(lengths[0])>MAX_BODY_BYTES:
        _error(413,'Draft request too large')
    body=bytearray()
    async for chunk in request.stream():
        if len(body)+len(chunk)>MAX_BODY_BYTES:
            _error(413,'Draft request too large')
        body.extend(chunk)
    if lengths and int(lengths[0])!=len(body):
        _error(400,'Invalid draft request framing')
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:
                raise ValueError()
            result[key]=value
        return result
    def reject_constant(value):
        raise ValueError()
    try:
        decoded=json.loads(body.decode('utf-8'),object_pairs_hook=unique,parse_constant=reject_constant)
        return schema.validate_python(decoded) if isinstance(schema,TypeAdapter) else schema.model_validate(decoded)
    except (ValueError,TypeError,UnicodeError,RecursionError):
        # Never serialize Pydantic errors: they echo the customer's input.
        _error(422,'Invalid draft request')


def _execute(request, resolver, operation, *, payload=None, draft_id=None, limit=50):
    service=_ready(request)
    permission=WorkshopPermission.READ_WORKSHOP_DRAFT if operation in ('list','read') else WorkshopPermission.WRITE_WORKSHOP_DRAFT
    context=resolver(request,permission)
    try:
        if operation=='create':
            if isinstance(payload,CreateInquiryDraft):
                result=service.create_work_inquiry(context,request_id=payload.request_id,payload=payload.payload)
            else:
                result=service.create_labour_entry(context,request_id=payload.request_id,payload=payload.payload,
                    work_id=payload.work_id,original_work_version=payload.original_work_version)
        elif operation=='save':
            result=service.save(context,draft_id,request_id=payload.request_id,
                                expected_revision=payload.expected_revision,payload=payload.payload)
        elif operation=='discard':
            result=service.discard(context,draft_id,request_id=payload.request_id,
                                   expected_revision=payload.expected_revision)
        elif operation=='read':
            result=service.read(context,draft_id)
        else:
            result=service.list(context,limit=limit)
    except DraftNotFoundError:
        _error(404,'Draft unavailable')
    except DraftRevisionConflictError:
        _error(409,'Draft revision conflict')
    except DraftIdempotencyConflictError:
        _error(409,'Draft request conflict')
    except DraftKeyConfigurationError:
        _error(503,'Draft service unavailable')
    except (PermissionDeniedError,WorkshopAccessDenied):
        _error(403,'Permission denied')
    except AuthenticationError:
        _error(401,'Authentication required')
    except sqlite3.DatabaseError:
        _error(503,'Draft service unavailable')
    except (ValueError,TypeError):
        _error(422,'Invalid draft request')
    try:
        if operation in ('list','read'):
            rows=result if operation=='list' else (result,)
            data=[SnapshotResponse.model_validate(
                      SnapshotResponse.model_validate(row).model_dump()|attribution).model_dump(mode='json')
                  for row,attribution in zip(rows,_attribution(request,context,rows))]
            if operation=='read':
                data=data[0]
        else:
            data=ReceiptResponse.model_validate(result).model_dump(mode='json')
    except (ValidationError,ValueError,TypeError):
        _error(503,'Draft service unavailable')
    return JSONResponse(data,status_code=201 if operation=='create' else 200,
                        headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})


def register_draft_routes(app, context_resolver):
    async def create_draft(request: Request):
        _ready(request)
        _query(request)
        payload=await _payload(request,CREATE_REQUEST)
        return await run_in_threadpool(_execute,request,context_resolver,'create',payload=payload)

    async def list_drafts(request: Request):
        _ready(request)
        limit=_query(request,listing=True)
        return await run_in_threadpool(_execute,request,context_resolver,'list',limit=limit)

    async def read_draft(request: Request):
        _ready(request)
        _query(request)
        return await run_in_threadpool(_execute,request,context_resolver,'read',draft_id=_draft_id(request))

    async def save_draft(request: Request):
        _ready(request)
        _query(request)
        draft_id=_draft_id(request)
        payload=await _payload(request,SaveDraft)
        return await run_in_threadpool(_execute,request,context_resolver,'save',draft_id=draft_id,payload=payload)

    async def discard_draft(request: Request):
        _ready(request)
        _query(request)
        draft_id=_draft_id(request)
        payload=await _payload(request,DiscardDraft)
        return await run_in_threadpool(_execute,request,context_resolver,'discard',draft_id=draft_id,payload=payload)

    for path,method,handler in (
        ('/api/drafts','POST',create_draft),('/api/drafts','GET',list_drafts),
        ('/api/drafts/{draft_id}','GET',read_draft),('/api/drafts/{draft_id}','PUT',save_draft),
        ('/api/drafts/{draft_id}/discard','POST',discard_draft),
    ):
        app.add_api_route(path,handler,methods=[method],include_in_schema=False)
