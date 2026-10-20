"""Private draft contracts, not HTTP request/response models."""

import json
from dataclasses import dataclass, field
from enum import Enum

from webapp.draft_migrations import MAX_PAYLOAD_BYTES


class DraftType(str, Enum):
    WORK_INQUIRY = 'WORK_INQUIRY'
    LABOUR_ENTRY = 'LABOUR_ENTRY'


class DraftNotFoundError(LookupError):
    def __init__(self):
        super().__init__('draft unavailable')


class DraftRevisionConflictError(ValueError):
    def __init__(self):
        super().__init__('draft revision conflict')


class DraftIdempotencyConflictError(ValueError):
    def __init__(self):
        super().__init__('draft request conflict')


FIELD_LIMITS = {
    DraftType.WORK_INQUIRY: {'registration':20, 'customer_description':4000},
    DraftType.LABOUR_ENTRY: {'hours':64, 'source':32, 'work_description':2000, 'autodata_reference':500},
}


def editor_json(draft_type, payload):
    """Normalize bounded editor values without final business validation."""
    if type(payload) is not dict or not isinstance(draft_type, DraftType):
        raise ValueError('invalid draft editor payload')
    limits = FIELD_LIMITS[draft_type]
    for key, value in payload.items():
        if (type(key) is not str or key not in limits or
                (value is not None and (type(value) is not str or len(value)>limits[key] or '\0' in value))):
            raise ValueError('invalid draft editor payload')
    try:
        result = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',',':'))
        if len(result.encode('utf-8'))>MAX_PAYLOAD_BYTES:
            raise ValueError('invalid draft editor payload')
    except UnicodeError:
        raise ValueError('invalid draft editor payload') from None
    return result


@dataclass(frozen=True)
class DraftReceipt:
    draft_id: str
    draft_type: DraftType
    revision: int
    state: str
    last_content_saved_at: int
    expires_at: int
    termination_reason: str | None = None
    terminated_at: int | None = None


@dataclass(frozen=True)
class DraftSnapshot:
    draft_id: str
    draft_type: DraftType
    revision: int
    payload: dict = field(repr=False)
    payload_schema_version: int = 1
    work_id: str | None = None
    original_work_version: int | None = None
    created_at: int = 0
    last_content_saved_at: int = 0
    expires_at: int = 0
    # Server-only attribution (never serialized to the browser): the creator, the
    # author of the latest content change (None when unknown), and their auth store.
    creator_id: str | None = None
    last_editor_id: str | None = None
    actor_store_id: str | None = None
