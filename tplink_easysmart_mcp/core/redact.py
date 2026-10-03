"""Recursive redaction of credential-bearing fields. Pure: always returns new objects.

The key policy is **rule-based**, not an exact-match allow/deny list, so a new
credential-bearing field name cannot leak merely because nobody added it here:

* an exact set of fully-spelled sensitive keys, and
* a substring set: any key whose lower-cased name *contains* one of these is
  redacted (so ``password``, ``cpassword``, ``new_password`` and ``H_P_SSID``
  are all covered without enumerating every spelling).

Field names that only *look* adjacent (``power_w``, ``pd_class``, ``portid``,
``name_ppowerlimit``) do not contain any rule token and survive untouched; the
tests pin that down so a future rule change cannot quietly eat port/PoE data.
"""

from __future__ import annotations

from typing import Any

REDACTED = "<redacted>"
TRUNCATED = "<truncated: max depth>"
MAX_DEPTH = 64

# Fully-spelled keys that are always secret even though they contain no token below.
_EXACT_KEYS = frozenset(
    {"password", "cpassword", "cookie", "h_p_ssid", "token", "secret", "authorization"}
)
# Any key whose lower-cased name contains one of these substrings is redacted.
_CONTAINS = ("pass", "pwd", "secret", "token", "cookie")


def is_sensitive_key(key: object) -> bool:
    name = str(key).lower()
    return name in _EXACT_KEYS or any(token in name for token in _CONTAINS)


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Return a copy of ``value`` with sensitive fields replaced by ``REDACTED``.

    Dicts and lists/tuples are rebuilt (tuples become lists, matching JSON).
    Nesting deeper than ``MAX_DEPTH`` is replaced by ``TRUNCATED``, which also
    terminates self-referencing structures.
    """
    if _depth >= MAX_DEPTH:
        return TRUNCATED
    if isinstance(value, dict):
        return {
            k: (REDACTED if is_sensitive_key(k) else redact(v, _depth=_depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item, _depth=_depth + 1) for item in value]
    return value
