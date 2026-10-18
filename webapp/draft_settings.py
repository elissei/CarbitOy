"""Explicit server opt-in for the draft HTTP API; no development secrets."""

import os
from dataclasses import dataclass, field

from webapp.draft_keys import DraftKeyring, DraftKeyConfigurationError


@dataclass(frozen=True)
class DraftApiSettings:
    enabled: bool = False
    keyring: DraftKeyring | None = field(default=None, repr=False)

    def __post_init__(self):
        if (type(self.enabled) is not bool
                or (self.enabled and not isinstance(self.keyring,DraftKeyring))
                or (not self.enabled and self.keyring is not None)):
            raise DraftKeyConfigurationError()

    @classmethod
    def from_environment(cls):
        flag = os.environ.get('CARBIT_DRAFT_API_ENABLED','0')
        if flag=='0':
            return cls()  # Ignore unused key configuration, never invent a key.
        if flag!='1':
            raise DraftKeyConfigurationError()
        return cls(enabled=True,keyring=DraftKeyring.from_environment(os.environ))
