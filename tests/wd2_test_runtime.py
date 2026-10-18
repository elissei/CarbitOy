"""Opt-in project-local temporary files: pytest -p tests.wd2_test_runtime.

Normal pytest invocations and production configuration are unchanged.
"""

import os
from pathlib import Path
import sys
import tempfile


def pytest_configure(config):
    root = Path(__file__).resolve().parents[1]
    scratch_root = root / ".test-tmp"
    if not scratch_root.resolve().is_relative_to(root):
        raise ValueError("WD-2 test temporary files must stay in the project")
    scratch_root.mkdir(exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="wd2-regression-", dir=scratch_root))
    if not scratch.resolve().is_relative_to(root):
        raise ValueError("WD-2 test temporary files must stay in the project")
    for name in ("TEMP", "TMP", "TMPDIR"):
        os.environ[name] = str(scratch)
    tempfile.tempdir = str(scratch)
    sys.dont_write_bytecode = True
    # Fresh child only; pytest may clear its basetemp on startup.
    config.option.basetemp = str(scratch / "pytest")
