"""Shared, strict tool-input types (device-agnostic; copied verbatim per repo).

``ConfirmWrite`` is the type every mutating tool uses for its ``confirm_write``
parameter. The vulnerability it closes (breaker finding TC-F6): a FastMCP tool
parameter typed plain ``bool`` lets pydantic *coerce* a JSON string ``"true"`` /
``"1"`` / ``"yes"`` to ``True`` **before** the write gate runs, so the gate's
``confirm_write is not True`` check — which exists precisely to reject truthy
strings — is defeated at the MCP boundary.

Fail closed: only the JSON boolean ``true`` confirms a write. Every other value
(a truthy or falsey string, a number, ``null``) resolves to ``False`` and is
refused by the write gate with the ordinary ``WRITE_REFUSED`` envelope and **no
network I/O** — never silently coerced through. The advertised JSON schema stays
``{"type": "boolean"}`` so a well-behaved client sends a real boolean.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BeforeValidator, Field

__all__ = ["ConfirmWrite"]


def _only_true_confirms(value: object) -> bool:
    """Only the boolean ``True`` authorises a write; everything else is ``False``."""
    return value is True


ConfirmWrite = Annotated[
    bool,
    BeforeValidator(_only_true_confirms),
    Field(
        default=False,
        description=(
            "Must be the JSON boolean true to authorise this mutating write. A string "
            'such as "true"/"1"/"yes" does NOT count and the write is refused with no '
            "network call; re-send with the boolean confirm_write=true after confirming "
            "the change with the operator."
        ),
    ),
]
