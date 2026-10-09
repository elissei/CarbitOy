"""Internal pricing of current human-approved labour; no HTTP or storage writes.

Snapshots describe a checked storage version, not a permanent approval. A
future quote service must revalidate them with require_current_snapshot and
bind quote persistence to the same inquiry version in its own transaction.
Parts are outside this service. Original LabourTime/LabourCost stay positive.
"""

from datetime import datetime, timezone
from decimal import Context, Decimal, ROUND_HALF_UP, localcontext
from typing import Literal

from pydantic import Field, field_serializer, field_validator, model_validator

from services.labour_pricing import (
    CENT, DEFAULT_HOURLY_RATE, VAT_RATE, split_inclusive_vat,
)
from webapp.database import VersionConflictError
from webapp.labour_models import StorageRecord, decimal_storage_text
from webapp.labour_service import (
    LabourPlanService, StaleLabourPlanError, TrustedWorkshopContext,
)


PRICING_RULES_VERSION = "web4c-v1"


def _money_context(hours: Decimal) -> Context:
    # Exact multiplication and enough quotient precision for cent rounding,
    # independent of a caller's Decimal precision, rounding or traps.
    precision = max(28, len(hours.as_tuple().digits) + 12, hours.adjusted() + 12)
    return Context(prec=precision, rounding=ROUND_HALF_UP)


def _labour_amounts(hours: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    with localcontext(_money_context(hours)):
        total = (hours * DEFAULT_HOURLY_RATE).quantize(CENT, rounding=ROUND_HALF_UP)
        if total.is_zero():
            total = total.copy_abs()
        # Match existing VAT: round the net, then subtract it from the gross.
        net, vat = split_inclusive_vat(total)
    return total, net, vat


class ApprovedLabourPricingRequest(StorageRecord):
    """Version/identity expectations only; billing hours come from storage."""

    inquiry_id: str = Field(min_length=1)
    expected_version: int = Field(ge=1, strict=True)
    expected_revision_id: str = Field(min_length=1)
    expected_content_id: str = Field(pattern="^[0-9a-f]{64}$")
    expected_rules_version: str = Field(min_length=1)


class ApprovedLabourPriceSnapshot(StorageRecord):
    """Immutable, serializable labour-only calculation, including zero charges.

    Merely constructing/deserializing this record grants no approval. Use the
    service's current-storage check before treating a saved result as current.
    """

    inquiry_id: str = Field(min_length=1)
    work_version: int = Field(ge=1, strict=True)
    revision_id: str = Field(min_length=1)
    content_id: str = Field(pattern="^[0-9a-f]{64}$")
    rules_version: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    pricing_rules_version: str = Field(min_length=1)
    original_total_hours: Decimal = Field(gt=0, allow_inf_nan=False)
    final_billable_hours: Decimal = Field(ge=0, allow_inf_nan=False)
    hourly_rate: Decimal = Field(gt=0, allow_inf_nan=False)
    vat_rate: Decimal = Field(ge=0, lt=1, allow_inf_nan=False)
    total_price: Decimal = Field(ge=0, allow_inf_nan=False)
    net_price: Decimal = Field(ge=0, allow_inf_nan=False)
    vat_price: Decimal = Field(ge=0, allow_inf_nan=False)
    currency: Literal["EUR"] = "EUR"
    created_at: datetime

    @field_validator("hourly_rate", "vat_rate", "total_price", "net_price", "vat_price", mode="before")
    @classmethod
    def require_exact_money(cls, value):
        if isinstance(value, (bool, float)):
            raise ValueError("use Decimal or decimal text, not bool or float")
        return value

    @field_serializer("hourly_rate", "vat_rate", "total_price", "net_price", "vat_price", when_used="json")
    def serialize_money(self, value: Decimal) -> str:
        return decimal_storage_text(value)

    @model_validator(mode="after")
    def validate_money_breakdown(self):
        with localcontext(_money_context(self.total_price)):
            if self.net_price + self.vat_price != self.total_price:
                raise ValueError("net price and VAT must equal the inclusive total")
            if any(value != value.quantize(CENT) for value in (
                self.total_price, self.net_price, self.vat_price,
            )):
                raise ValueError("money must be rounded to cents")
        return self


class ApprovedLabourPricingService:
    def __init__(self, labour_plans: LabourPlanService):
        self.labour_plans = labour_plans

    def price_approved_labour(
        self, context: TrustedWorkshopContext, request: ApprovedLabourPricingRequest,
    ) -> ApprovedLabourPriceSnapshot:
        request = ApprovedLabourPricingRequest.model_validate(request.model_dump(round_trip=True))
        # Read immutable source/decision metadata first. The subsequent gate
        # checks the same version, so a concurrent service write cannot mix
        # metadata from one decision with the hours of another decision.
        stored = self.labour_plans.get_active_plan(context, request.inquiry_id)
        if stored.work_version != request.expected_version:
            raise VersionConflictError("work changed since pricing was requested")
        hours = self.labour_plans.require_approved_final_hours(context, **request.model_dump())
        if stored.decision_id is None:
            raise StaleLabourPlanError("pricing requires a current persisted decision")
        total, net, vat = _labour_amounts(hours)
        return ApprovedLabourPriceSnapshot(
            inquiry_id=request.inquiry_id, work_version=stored.work_version,
            revision_id=stored.revision.revision_id, content_id=stored.revision.content_id,
            rules_version=stored.revision.rules_version, decision_id=stored.decision_id,
            pricing_rules_version=PRICING_RULES_VERSION,
            original_total_hours=stored.revision.original_total_hours,
            final_billable_hours=hours, hourly_rate=DEFAULT_HOURLY_RATE, vat_rate=VAT_RATE,
            total_price=total, net_price=net, vat_price=vat, created_at=datetime.now(timezone.utc),
        )

    def require_current_snapshot(
        self, context: TrustedWorkshopContext, snapshot: ApprovedLabourPriceSnapshot,
    ) -> ApprovedLabourPriceSnapshot:
        """Recheck storage and pricing policy; a historical snapshot is no authority.

        This read check does not reserve a version for later writes. Future
        quote persistence must atomically enforce the returned work_version.
        """
        snapshot = ApprovedLabourPriceSnapshot.model_validate(snapshot.model_dump(round_trip=True))
        current = self.price_approved_labour(context, ApprovedLabourPricingRequest(
            inquiry_id=snapshot.inquiry_id, expected_version=snapshot.work_version,
            expected_revision_id=snapshot.revision_id, expected_content_id=snapshot.content_id,
            expected_rules_version=snapshot.rules_version,
        ))
        if snapshot.model_dump(exclude={"created_at"}) != current.model_dump(exclude={"created_at"}):
            raise StaleLabourPlanError("price snapshot no longer matches current approval and pricing")
        return snapshot
