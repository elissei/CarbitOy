from pydantic import BaseModel, ConfigDict


class QuotePresentationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_description: str
