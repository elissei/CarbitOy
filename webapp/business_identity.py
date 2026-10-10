"""Server-captured attribution snapshots; constructing one is not authentication."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class BusinessActor:
    user_id: UUID
    display_name: str

    def __post_init__(self):
        if not isinstance(self.user_id, UUID):
            raise ValueError("a canonical auth-user UUID is required")
        if not isinstance(self.display_name, str) or not self.display_name.strip() or len(self.display_name) > 200:
            raise ValueError("a bounded historical display-name snapshot is required")

    def attribution(self) -> dict:
        return {"version": 1, "provenance": "authenticated_session",
                "user_id": str(self.user_id), "display_name": self.display_name}
