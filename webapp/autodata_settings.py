"""Explicit server opt-in for Autodata lookups (1B-2); disabled by default.

The environment can only select the disabled mode. The fake mode exists for
explicitly constructed test settings, and the browser mode is rejected until
a real adapter (1C) exists. One lookup runs at a time per process, so an
enabled mode requires exactly one declared server worker; the actual worker
count is checked when the service is wired (1B-3).
"""

import os
from dataclasses import dataclass
from enum import Enum


class AutodataConfigurationError(ValueError):
    pass


class AutodataMode(str, Enum):
    DISABLED = "disabled"
    FAKE = "fake"
    BROWSER = "browser"


@dataclass(frozen=True)
class AutodataSettings:
    mode: AutodataMode = AutodataMode.DISABLED
    # Synthetic (also fake) evidence may be accepted into an inquiry only in tests.
    allow_synthetic_acceptance: bool = False
    workers: int = 1

    def __post_init__(self):
        if (not isinstance(self.mode, AutodataMode)
                or type(self.allow_synthetic_acceptance) is not bool
                or type(self.workers) is not int or self.workers < 1):
            raise AutodataConfigurationError("invalid autodata settings")
        if self.mode is AutodataMode.BROWSER:
            raise AutodataConfigurationError("the browser mode requires the real adapter (1C)")
        if self.mode is not AutodataMode.DISABLED and self.workers != 1:
            raise AutodataConfigurationError("autodata lookups require exactly one server worker")
        if self.mode is AutodataMode.DISABLED and self.allow_synthetic_acceptance:
            raise AutodataConfigurationError("synthetic acceptance requires an enabled mode")

    @property
    def enabled(self) -> bool:
        return self.mode is not AutodataMode.DISABLED

    @classmethod
    def from_environment(cls):
        mode = os.environ.get("CARBIT_AUTODATA_MODE", AutodataMode.DISABLED.value)
        if mode != AutodataMode.DISABLED.value:
            # fake: explicit test settings only; browser: no real adapter yet.
            raise AutodataConfigurationError("CARBIT_AUTODATA_MODE must be 'disabled'")
        return cls()
