"""Autodata 1B persistence helpers: canonical digests, timestamps and minimisation.

Registration, VIN and free search text are stored in the redactable inputs
table. Texts entering immutable JSON pass the plan 2.6 heuristic, which blocks
known forms only; it is no guarantee that the JSON holds no VIN or personal
data (structural removability is task 1B-2.0).
"""

import json
import re
from datetime import datetime, timezone
from hashlib import sha256
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from integrations.autodata.models import (
    ChoiceIdentity, LabourTimeLookupResult, LabourTimeRequest, LookupOutcome,
)
from webapp.autodata_migrations import MAX_RESULT_JSON_BYTES


LOOKUP_CONTRACT = "carbit.autodata.lookup.authenticated.v1"
ACCEPT_CONTRACT = "carbit.autodata.accept.authenticated.v1"
MAX_WORK_DESCRIPTION = 2000
_VIN_LIKE = re.compile(r"[A-HJ-NPR-Z0-9]{17}")
_SEPARATORS = re.compile(r"[\s\-]+")
_TOKEN_SPLIT = re.compile(r"[^A-Z0-9]+")


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def utc_micro(moment: datetime) -> str:
    """Fixed-width UTC text; the schema rejects every other timestamp form."""
    if moment.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_utc(text: str) -> datetime:
    return datetime.fromisoformat(text)


def canonical_uuid(value) -> str:
    return str(UUID(str(value)))


def identity_json(identity: ChoiceIdentity) -> str:
    return canonical_json(identity.model_dump(mode="json"))


def command_digest(contract: str, *, actor_user_id: str, inquiry_id: str, body: dict,
                   lookup_id: str | None = None) -> str:
    """Only the client's own command: path parameters and body, no server values."""
    command = {"contract": contract, "actor_user_id": actor_user_id,
               "inquiry_id": inquiry_id, "body": body}
    if lookup_id is not None:
        command["lookup_id"] = lookup_id
    return digest(command)


def _tokens(text: str) -> list[str]:
    return [token for token in _TOKEN_SPLIT.split(text.upper()) if token]


def contains_known_identifier(text: str, identifier: str | None) -> bool:
    """Does text contain this vehicle's known registration or VIN?

    Four or more normalised characters: substring of the text without spaces
    and hyphens ("abc 123" finds ABC-123). Shorter (e.g. AB-1 -> AB1): only
    whole consecutive tokens that join to exactly the identifier ("AB-1",
    "ab 1"), so ordinary words containing the letters ("LAB1", "ABS 1") pass.
    """
    if identifier is None:
        return False
    target = "".join(_tokens(identifier))
    if not target:
        return False
    if len(target) >= 4:
        return target in _SEPARATORS.sub("", text).upper()
    tokens = _tokens(text)
    for start in range(len(tokens)):
        joined = ""
        for token in tokens[start:]:
            joined += token
            if len(joined) >= len(target):
                break
        if joined == target:
            return True
    return False


def _vin_windows(text: str):
    for start in range(len(text) - 16):
        window = text[start:start + 17]
        if _VIN_LIKE.fullmatch(window):
            yield window


def contains_vin_like(text: str) -> bool:
    """A VIN-shaped value even when no VIN was entered for this vehicle.

    1. Contiguous (plan 2.6): any 17 consecutive VIN-alphabet characters inside
       one token, with prefix or suffix allowed ("VINWVWZZZ1JZXW000001"), that
       contain a digit. The digit is a refinement of the plan's wording, so
       long Finnish compounds ("etujarrupalasarjat") are not rejected.
    2. Split by separators: any 17-character window of the text with all
       separators removed, also starting or ending inside a token, whose last
       four characters are digits ("VINWVW ZZZ 1JZ XW 000001FI"). Without that
       ending, ordinary text such as "Etujarrupalat 2 kpl" would be rejected.
    Limit: a VIN split by separators whose last four characters are not all
    digits stays undetected. This is a heuristic, not complete PII protection.
    """
    tokens = _tokens(text)
    for token in tokens:
        if any(any(char.isdigit() for char in window) for window in _vin_windows(token)):
            return True
    return any(window[-4:].isdigit() for window in _vin_windows("".join(tokens)))


def search_text_has_identifier(search_text: str, *, registration: str, vin: str | None) -> bool:
    """Known registration or VIN, or any VIN-shaped value (see the two rules above).

    Not a general personal-data detector: names or other free text pass.
    """
    return (contains_known_identifier(search_text, registration)
            or contains_known_identifier(search_text, vin) or contains_vin_like(search_text))


def _snapshot_texts(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _snapshot_texts(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _snapshot_texts(item)


def snapshot_has_identifier(snapshot: dict, *, registration: str, vin: str | None) -> bool:
    """Every text that would enter immutable snapshot_json, not just its keys."""
    return any(search_text_has_identifier(text, registration=registration, vin=vin)
               for text in _snapshot_texts(snapshot))


def _result_texts(result: LabourTimeLookupResult):
    choices = [resolved.choice for resolved in (result.vehicle, result.operation, result.variant)
               if resolved is not None] + list(result.candidates)
    for choice in choices:
        yield from choice.identity.selection_path
        yield from choice.details
        if choice.identity.source_id is not None:
            yield choice.identity.source_id
    if result.time is not None:
        yield result.time.raw_time_text
    if result.rejected_time_text is not None:
        yield result.rejected_time_text


def minimise_result(result: LabourTimeLookupResult, *, registration: str,
                    vin: str | None) -> LabourTimeLookupResult:
    """Never store source labels echoing the vehicle's identifiers, nor oversize results.

    Such a result becomes SITE_CHANGED without choices or time; masking would
    break the identity that a later selection must replay.
    """
    leaked = any(search_text_has_identifier(text, registration=registration, vin=vin)
                 for text in _result_texts(result))
    oversize = len(canonical_json(result.model_dump(mode="json")).encode("utf-8")) > MAX_RESULT_JSON_BYTES
    if leaked or oversize:
        return LabourTimeLookupResult(outcome=LookupOutcome.SITE_CHANGED, provenance=result.provenance)
    return result


def split_request(request: LabourTimeRequest) -> tuple[dict, dict]:
    """Return (non-personal snapshot, redactable inputs) of a server-completed request."""
    facts = request.vehicle_query.model_dump(mode="json", exclude={"registration", "vin"})
    snapshot = {
        "vehicle_facts": facts,
        "selections": {
            name: getattr(request, name).model_dump(mode="json") if getattr(request, name) else None
            for name in ("vehicle_selection", "operation_selection", "variant_selection")
        },
    }
    inputs = {
        "registration": request.vehicle_query.registration,
        "vin": request.vehicle_query.vin,
        "search_text": request.work_query.search_text,
    }
    return snapshot, inputs


class AcceptLookupCommand(BaseModel):
    """Staff acceptance; hours never come from the browser."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str
    expected_version: int = Field(ge=1, strict=True)
    expected_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    vehicle_identity: ChoiceIdentity
    operation_identity: ChoiceIdentity
    variant_identity: ChoiceIdentity
    confirmation: Literal["VEHICLE_OPERATION_VARIANT_TIME"]

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, value: str) -> str:
        return canonical_uuid(value)

    def body(self) -> dict:
        return self.model_dump(mode="json")
