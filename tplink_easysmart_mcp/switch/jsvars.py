"""Brace- and quote-aware scanner for the switch's inline ``var NAME = VALUE;`` blocks.

Every ``*Rpm.htm`` page and the login page start with one ``<script>`` block of
``var`` declarations, then UI HTML. The reference client
(``vmakeev/hass_tplink_easy_smart``, MIT, (c) 2022 Vladimir Makeev) parses these
with ``var\\s+(\\w+)\\s*=\\s*([^;]+);`` + ``json5``; that regex truncates on a
``;`` inside a string value (e.g. the switch description). This scanner is pure
and string-aware: it reads only the first ``<script>`` block, tracks brace,
bracket, paren depth and quotes, converts each value to JSON text and parses it
with ``json.loads`` (no ``json5`` dependency, and ``0x`` hex is handled).

Logic is ported from the reference; none of its files are copied.
"""

from __future__ import annotations

import json
import re

from .constants import (
    MAX_ARRAY_ITEMS,
    MAX_DECLARED_PORTS,
    MAX_DEPTH,
    MAX_IDENT_LEN,
    MAX_INPUT_BYTES,
)
from .errors import ProtocolError

JsValue = None | bool | int | float | str | list["JsValue"] | dict[str, "JsValue"]

_SCRIPT_OPEN = re.compile(r"<script\b[^>]*>", re.IGNORECASE)
_WS = " \t\r\n"


def extract_vars(html: str) -> dict[str, JsValue]:
    """Return ``{name: value}`` for the first ``<script>`` block's ``var`` statements.

    Returns an empty dict when there is no script block. Raises ``ProtocolError``
    for oversized input, NUL bytes, over-deep nesting, over-long arrays, an
    unterminated value/string, or any token the grammar does not allow. Pure:
    never mutates its input and always returns a fresh dict.
    """
    if len(html) > MAX_INPUT_BYTES:
        raise ProtocolError(f"page too large: {len(html)} bytes > {MAX_INPUT_BYTES}")
    if "\x00" in html:
        raise ProtocolError("page contains NUL bytes")
    text = html.lstrip("﻿")
    inner = _first_script(text)
    if inner is None:
        return {}
    return _parse_script(inner)


def declared_count(data: dict[str, JsValue], key: str) -> int:
    """Read a page-declared length/count and cap it at ``MAX_DECLARED_PORTS``.

    Every per-port/per-entry count a parser reads to size a loop (``portNum``,
    ``max_port_num``, ``poe_port_num``, any ``*_num``) must go through here. A
    scalar count escapes the array-size cap that already guards declared arrays,
    so without this a hostile page could set ``portNum = 5_000_000`` and drive an
    O(n) parser loop into a CPU DoS. Raises ``ProtocolError`` if the value is
    missing, not an int, negative, or above the cap — before any loop runs.
    """
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"{key} is missing or not an integer")
    if value < 0:
        raise ProtocolError(f"{key}={value} is negative")
    if value > MAX_DECLARED_PORTS:
        raise ProtocolError(f"{key}={value} exceeds MAX_DECLARED_PORTS={MAX_DECLARED_PORTS}")
    return value


def _first_script(html: str) -> str | None:
    match = _SCRIPT_OPEN.search(html)
    if match is None:
        return None
    start = match.end()
    # A literal </script> ends the block; an escaped <\/script> inside a string
    # keeps its backslash and so does not match here.
    end = html.lower().find("</script>", start)
    return html[start:] if end == -1 else html[start:end]


def _parse_script(inner: str) -> dict[str, JsValue]:
    result: dict[str, JsValue] = {}
    i, n = 0, len(inner)
    while i < n:
        while i < n and inner[i] in _WS + ";":
            i += 1
        if i >= n:
            break
        if not (inner.startswith("var", i) and i + 3 < n and inner[i + 3] in _WS):
            break  # the first block holds only var declarations; stop at anything else
        i += 3
        while i < n and inner[i] in _WS:
            i += 1
        j = i
        while j < n and (inner[j].isalnum() or inner[j] == "_"):
            j += 1
        ident = inner[i:j]
        if not ident or not (ident[0].isalpha() or ident[0] == "_"):
            raise ProtocolError("expected an identifier after 'var'")
        if len(ident) > MAX_IDENT_LEN:
            raise ProtocolError(f"identifier too long: {len(ident)} chars")
        i = j
        while i < n and inner[i] in _WS:
            i += 1
        if i >= n or inner[i] != "=":
            raise ProtocolError(f"expected '=' after var {ident!r}")
        raw, i = _read_raw_value(inner, i + 1)
        result[ident] = _value_to_python(raw)
    return result


def _read_raw_value(s: str, i: int) -> tuple[str, int]:
    """Read one value, terminating on ``;`` or newline at depth 0 (both consumed)."""
    n, start, depth, in_str = len(s), i, 0, ""
    while i < n:
        c = s[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = ""
            i += 1
            continue
        if c in "\"'":
            in_str = c
        elif c in "[{(":
            depth += 1
        elif c in "]})":
            depth -= 1
        elif depth == 0 and c in ";\n":
            return s[start:i], i + 1
        i += 1
    if depth != 0 or in_str:
        raise ProtocolError("unterminated inline value")
    return s[start:i], i


def _value_to_python(raw: str) -> JsValue:
    try:
        return json.loads(_to_json(raw.strip()))
    except RecursionError as exc:  # pragma: no cover - depth guard fires first
        raise ProtocolError("inline value nested too deep") from exc
    except ValueError as exc:
        raise ProtocolError("could not parse inline value") from exc


def _kw_boundary(s: str, i: int, length: int) -> bool:
    before = s[i - 1] if i > 0 else " "
    after = s[i + length] if i + length < len(s) else " "
    return not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_")


def _to_json(raw: str) -> str:  # one linear scanner, kept together on purpose
    out: list[str] = []
    i, n, depth = 0, len(raw), 0
    comma_counts: list[int] = []
    is_array_paren: list[bool] = []

    def _open(token: str, array_paren: bool) -> None:
        nonlocal depth
        out.append(token)
        depth += 1
        if depth > MAX_DEPTH:
            raise ProtocolError(f"nesting deeper than {MAX_DEPTH}")
        comma_counts.append(0)
        is_array_paren.append(array_paren)

    def _close(token: str) -> None:
        nonlocal depth
        out.append(token)
        if comma_counts:
            comma_counts.pop()
        if is_array_paren:
            is_array_paren.pop()
        depth -= 1

    while i < n:
        c = raw[i]
        if c in "\"'":
            content, i = _read_string(raw, i)
            out.append(json.dumps(content))
            continue
        if c == "n" and raw.startswith("new", i) and _kw_boundary(raw, i, 3):
            k = i + 3
            while k < n and raw[k] in _WS:
                k += 1
            if raw.startswith("Array", k) and _kw_boundary(raw, k, 5):
                k += 5
                while k < n and raw[k] in _WS:
                    k += 1
                if k < n and raw[k] == "(":
                    _open("[", array_paren=True)
                    i = k + 1
                    continue
            raise ProtocolError("unexpected 'new' token")
        if c == "(":
            raise ProtocolError("unexpected '(' in value")
        if c == ")":
            if is_array_paren and is_array_paren[-1]:
                _close("]")
                i += 1
                continue
            raise ProtocolError("unbalanced ')' in value")
        if c in "[{":
            _open(c, array_paren=False)
            i += 1
            continue
        if c in "]}":
            _close(c)
            i += 1
            continue
        if c == ",":
            j = i + 1
            while j < n and raw[j] in _WS:
                j += 1
            if j < n and raw[j] in "]}":
                i = j  # drop a trailing comma
                continue
            if comma_counts:
                comma_counts[-1] += 1
                if comma_counts[-1] + 1 > MAX_ARRAY_ITEMS:
                    raise ProtocolError(f"more than {MAX_ARRAY_ITEMS} items in a container")
            out.append(",")
            i += 1
            continue
        if c == "0" and i + 1 < n and raw[i + 1] in "xX":
            k = i + 2
            while k < n and raw[k] in "0123456789abcdefABCDEF":
                k += 1
            if k == i + 2:
                raise ProtocolError("malformed hex literal")
            out.append(str(int(raw[i:k], 16)))
            i = k
            continue
        if c.isalpha() or c == "_":
            k = i
            while k < n and (raw[k].isalnum() or raw[k] == "_"):
                k += 1
            ident = raw[i:k]
            m = k
            while m < n and raw[m] in _WS:
                m += 1
            if m < n and raw[m] == ":":
                if len(ident) > MAX_IDENT_LEN:
                    raise ProtocolError(f"key too long: {len(ident)} chars")
                out.append(json.dumps(ident))
                out.append(":")
                i = m + 1
                continue
            low = ident.lower()
            if low in ("true", "false", "null"):
                out.append(low)
                i = k
                continue
            raise ProtocolError(f"unexpected token {ident!r}")
        if c in "0123456789+-.eE: \t\r\n":
            out.append(c)
            i += 1
            continue
        raise ProtocolError(f"unexpected character {c!r} in value")
    return "".join(out)


def _read_string(s: str, i: int) -> tuple[str, int]:
    """Read a single- or double-quoted string, resolving escapes to real chars."""
    quote = s[i]
    i += 1
    n = len(s)
    buf: list[str] = []
    simple = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}
    while i < n:
        c = s[i]
        if c == "\\":
            if i + 1 >= n:
                raise ProtocolError("dangling escape in string")
            nxt = s[i + 1]
            if nxt == "u":
                code = s[i + 2 : i + 6]
                if len(code) < 4:
                    raise ProtocolError("bad \\u escape")
                try:
                    buf.append(chr(int(code, 16)))
                except ValueError as exc:
                    raise ProtocolError("bad \\u escape") from exc
                i += 6
                continue
            if nxt == "x":
                code = s[i + 2 : i + 4]
                try:
                    buf.append(chr(int(code, 16)))
                except ValueError as exc:
                    raise ProtocolError("bad \\x escape") from exc
                i += 4
                continue
            buf.append(simple.get(nxt, nxt))
            i += 2
            continue
        if c == quote:
            return "".join(buf), i + 1
        buf.append(c)
        i += 1
    raise ProtocolError("unterminated string value")
