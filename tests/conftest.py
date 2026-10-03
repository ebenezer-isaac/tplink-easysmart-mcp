"""Shared test setup: put the ``vigi_nvr_mcp`` conformance shim on ``sys.path``.

The canonical breaker conformance suite is copied verbatim (so the core-identity
test can hash it) and imports the canonical package name ``vigi_nvr_mcp``. That
name is provided by a test-only alias under ``tests/_vigi_shim`` which re-exports
this repo's byte-identical ``core``. Inserting the directory here makes it
importable in the pytest process; a ``multiprocessing`` ``spawn`` child inherits
the parent's ``sys.path``, so the concurrency cases resolve it too.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SHIM = Path(__file__).resolve().parent / "_vigi_shim"
if str(_SHIM) not in sys.path:
    sys.path.insert(0, str(_SHIM))
