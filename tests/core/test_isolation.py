"""core/ is copied verbatim into sibling repos, so it must stay device-agnostic.

These two checks (ported from the template's own suite) guard that: core imports
nothing from the rest of the package, and mentions no device name. Keep them
passing unchanged.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[2] / "tplink_easysmart_mcp" / "core"
PACKAGE_NAME = "tplink_easysmart_mcp"
DEVICE_WORDS = ("easysmart", "tplink", "tp-link", "sg1016", "vigi", "nvr", "stok=<")


def test_core_imports_nothing_from_the_package() -> None:
    for path in CORE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level >= 2:
                    pytest.fail(f"{path.name} imports outside core: level {node.level}")
                if node.module and node.module.startswith(PACKAGE_NAME):
                    pytest.fail(f"{path.name} imports {node.module}")
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith(PACKAGE_NAME) for a in node.names)


def test_core_is_device_agnostic() -> None:
    for path in CORE.glob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        for word in DEVICE_WORDS:
            assert word not in text, f"{path.name} mentions {word!r}"
