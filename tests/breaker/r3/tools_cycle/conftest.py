"""Path + import shim for the r3 tools/cycle breaker vector.

These tests reuse the stateful fakes that ship with the suite
(``tests/switch_fakes.py``). To import them as ``tests.switch_fakes`` the repo
root must be on ``sys.path``; pytest's per-directory import mode does not
guarantee that for a test nested this deep, so we insert it here. The same two
directories are also put on the path so the ``multiprocessing`` (spawn) child in
``test_marker_multiprocess_race`` can import the worker module by its bare name.
"""

from __future__ import annotations

import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[3]  # tools_cycle -> r3 -> breaker -> tests -> repo root

for _p in (str(_REPO_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
