from __future__ import annotations

from pathlib import Path


_SRC_PACKAGE_DIR = Path(__file__).resolve().parent.parent / "src" / "agent_cv"

if not _SRC_PACKAGE_DIR.is_dir():
    raise ModuleNotFoundError(f"Expected package directory not found: {_SRC_PACKAGE_DIR}")

__path__ = [str(_SRC_PACKAGE_DIR)]
