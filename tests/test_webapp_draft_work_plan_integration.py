"""WD-to-web compatibility; requires the separate DRAFT/WORKSHOP implementation.

Excluded from the WD-only checkpoint. Test bodies retain their original assertions.
"""

import pytest

from app.work_plan_adapters import to_domain_labour_plan, to_domain_work_plan
from app.work_plan_contract import SourceRevision
from domain.models.labour_time import LabourTimeSource
from webapp.draft_models import DraftSnapshot, DraftType, editor_json
from webapp.labour_models import OriginalWorkTimeRecord
from webapp.labour_service import build_storage_labour_plan

from test_work_plan_contract import snapshot, time, work


def test_v1_drafts_keep_their_payload_shape_and_schema_version():
    payload = {"registration": "TEST-123", "customer_description": "Unidentified noise"}
    before = editor_json(DraftType.WORK_INQUIRY, payload)
    legacy = DraftSnapshot("draft-1", DraftType.WORK_INQUIRY, 8, payload)
    assert legacy.payload_schema_version == 1
    new = snapshot(source_revision=SourceRevision(kind="V1_DRAFT", source_id="draft-1", revision=8))
    assert new.schema_version == 2 and new.revision == 1 and new.works == ()
    assert editor_json(DraftType.WORK_INQUIRY, legacy.payload) == before
    with pytest.raises(ValueError):
        editor_json(DraftType.WORK_INQUIRY, {"works": []})
    assert editor_json(DraftType.LABOUR_ENTRY, {"hours": "", "source": None})


def test_current_storage_labour_contract_has_identical_domain_projection():
    plan = snapshot(works=(work(),), original_times=(time(),), selected_time_ids=("pair-time",))
    domain = to_domain_labour_plan(plan)
    legacy = build_storage_labour_plan(to_domain_work_plan(plan), [OriginalWorkTimeRecord(
        work_item_id="brakes", operation_identifier="BRAKE_REPLACEMENT", axle="FRONT", side="BOTH",
        original_hours="1.60", source=LabourTimeSource.MANUAL_AUTODATA,
        work_description="Full pair replacement", recorded_by="employee-1", autodata_reference="TEST-REF",
    )])
    assert legacy.require_total_hours() == domain.require_total_hours()
    assert legacy.review_status == domain.review_status
    assert legacy.work_plan == domain.work_plan
