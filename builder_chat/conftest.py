"""Pytest path bootstrap for the Builder Chat package."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _REPO_ROOT / "backend"

for path in (str(_REPO_ROOT), str(_BACKEND)):
    if path not in sys.path:
        sys.path.insert(0, path)
