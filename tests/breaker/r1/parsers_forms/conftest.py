"""Shared helpers for the r1 parsers/forms breaker vector.

Pure: these tests never touch the network. They load the synthetic fixtures that
ship with the repo and mutate copies of them in memory to model hostile — but
structurally valid — switch responses.
"""

from __future__ import annotations

import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).resolve().parents[3] / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def fixtures_dir() -> pathlib.Path:
    return FIXTURES
