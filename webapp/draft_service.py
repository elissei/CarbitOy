"""Internal draft service. No HTTP, conversion, submission or collaboration."""

from webapp.draft_models import DraftType
from webapp.draft_repository import DraftRepository


class DraftService:
    def __init__(self, repository: DraftRepository):
        if not isinstance(repository,DraftRepository):
            raise TypeError('a private draft repository is required')
        self.repository = repository

    def create_work_inquiry(self, context, *, request_id, payload):
        return self.repository.create(context,draft_type=DraftType.WORK_INQUIRY,request_id=request_id,payload=payload)

    def create_labour_entry(self, context, *, work_id, original_work_version, request_id, payload):
        return self.repository.create(context,draft_type=DraftType.LABOUR_ENTRY,work_id=work_id,
            original_work_version=original_work_version,request_id=request_id,payload=payload)

    def read(self, context, draft_id):
        return self.repository.read(context,draft_id)

    def list(self, context, *, limit=50):
        return self.repository.list(context,limit=limit)

    def save(self, context, draft_id, *, expected_revision, request_id, payload):
        return self.repository.save(context,draft_id,expected_revision=expected_revision,request_id=request_id,payload=payload)

    def discard(self, context, draft_id, *, expected_revision, request_id):
        return self.repository.discard(context,draft_id,expected_revision=expected_revision,request_id=request_id)

    def cleanup_expired(self, context, *, batch_size=100):
        return self.repository.cleanup_expired(context,batch_size=batch_size)
