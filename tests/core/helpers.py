"""Shared constants for the device-agnostic core tests.

The core package is copied verbatim from the template, so these tests exercise it
exactly as the template's own tests do, only with this repo's env prefix. All
addresses are RFC 5737 examples; the password is a dummy.
"""

from __future__ import annotations

PREFIX = "EASYSMART_"
MCP_PREFIX = "EASYSMART_MCP_"
DOC_HOST = "192.0.2.10"  # RFC 5737 TEST-NET-1
TEST_PASSWORD = "TestPass123"


def env(**overrides: str) -> dict[str, str]:
    base = {f"{PREFIX}HOST": DOC_HOST, f"{PREFIX}PASSWORD": TEST_PASSWORD}
    return {**base, **{f"{PREFIX}{k}": v for k, v in overrides.items()}}
