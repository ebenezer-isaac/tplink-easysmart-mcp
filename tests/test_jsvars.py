"""Edge and adversarial tests for the inline-variable scanner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tplink_easysmart_mcp.switch.constants import MAX_ARRAY_ITEMS, MAX_INPUT_BYTES
from tplink_easysmart_mcp.switch.errors import ProtocolError
from tplink_easysmart_mcp.switch.jsvars import extract_vars

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MANIFEST = json.loads((FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))
FIXTURE_NAMES = sorted(MANIFEST["fixtures"])


def _fx(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _script(body: str) -> str:
    return f"<script>\n{body}\n</script><html></html>"


# ---- the 18 fixtures all parse without raising ------------------------------


def test_manifest_lists_every_fixture_file() -> None:
    on_disk = {p.name for p in FIXTURES.glob("*.html")}
    assert set(FIXTURE_NAMES) == on_disk


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_every_fixture_parses(name: str) -> None:
    result = extract_vars(_fx(name))
    assert isinstance(result, dict)


# ---- the hostile description -------------------------------------------------


def test_hostile_description_exact_and_no_leaked_var() -> None:
    result = extract_vars(_fx("system_info_hostile_description.html"))
    assert result["info_ds"]["descriStr"][0] == 'lab;sw "q" var x=1; </script>'
    assert "x" not in result  # the `var x=1` inside the string is not a statement


# ---- value forms -------------------------------------------------------------


def test_semicolon_inside_string() -> None:
    assert extract_vars(_script('var a = "x;y;z";'))["a"] == "x;y;z"


def test_escaped_quotes() -> None:
    assert extract_vars(_script('var a = "he said \\"hi\\"";'))["a"] == 'he said "hi"'


def test_single_quoted_strings_and_arrays() -> None:
    assert extract_vars(_script("var a = ['Default','cams'];"))["a"] == ["Default", "cams"]


def test_hex_values() -> None:
    out = extract_vars(_script("var a = [0x0,0x8000,0xff00];"))["a"]
    assert out == [0, 32768, 65280]


def test_trailing_commas_dropped() -> None:
    assert extract_vars(_script("var a = [1,2,3,];var b = {x:1,};"))["a"] == [1, 2, 3]
    assert extract_vars(_script("var b = {x:1,};"))["b"] == {"x": 1}


def test_new_array_multiline() -> None:
    assert extract_vars(_script("var l = new Array(\n0,\n0,0);"))["l"] == [0, 0, 0]


def test_several_statements_one_line() -> None:
    out = extract_vars(_script('var a = {k:1};var tip = "";'))
    assert out["a"] == {"k": 1} and out["tip"] == ""


def test_crlf_line_endings() -> None:
    body = "var a = 1;\r\nvar b = 2;\r\n"
    assert extract_vars(f"<script>\r\n{body}</script>") == {"a": 1, "b": 2}


def test_bom_is_tolerated() -> None:
    assert extract_vars("﻿" + _script("var a = 1;"))["a"] == 1


def test_unicode_in_names() -> None:
    assert extract_vars(_script('var a = ["caf\\u00e9","\\u0448"];'))["a"] == ["café", "ш"]


# ---- empty / absent / malformed ---------------------------------------------


def test_empty_page_returns_empty_dict() -> None:
    assert extract_vars("") == {}


def test_no_script_block_returns_empty_dict() -> None:
    assert extract_vars("<html><body>hi</body></html>") == {}


def test_unterminated_object_raises() -> None:
    with pytest.raises(ProtocolError):
        extract_vars(_script("var a = {x:1"))


def test_unterminated_string_raises() -> None:
    with pytest.raises(ProtocolError):
        extract_vars('<script>var a = "oops</script>')


def test_bare_word_value_rejected() -> None:
    with pytest.raises(ProtocolError):
        extract_vars(_script("var a = somefunc();"))


def test_new_not_followed_by_array_rejected() -> None:
    with pytest.raises(ProtocolError, match="new"):
        extract_vars(_script("var a = new Foo(1);"))


def test_unexpected_character_rejected() -> None:
    with pytest.raises(ProtocolError, match="unexpected character"):
        extract_vars(_script("var a = @;"))


def test_malformed_hex_rejected() -> None:
    with pytest.raises(ProtocolError, match="hex"):
        extract_vars(_script("var a = 0xZZ;"))


def test_object_key_too_long_rejected() -> None:
    with pytest.raises(ProtocolError, match="too long"):
        extract_vars(_script(f"var a = {{{'k' * 65}:1}};"))


def test_x_escape_resolved() -> None:
    assert extract_vars(_script('var a = "\\x41";'))["a"] == "A"


def test_bad_u_escape_rejected() -> None:
    with pytest.raises(ProtocolError):
        extract_vars(_script('var a = "\\uZZZZ";'))


def test_bad_x_escape_rejected() -> None:
    with pytest.raises(ProtocolError):
        extract_vars(_script('var a = "\\xZZ";'))


def test_boolean_and_null_values() -> None:
    out = extract_vars(_script("var a = true;var b = false;var c = null;"))
    assert out == {"a": True, "b": False, "c": None}


def test_missing_closing_script_tag_still_parses() -> None:
    assert extract_vars("<script>var a = 1;var b = 2;")["b"] == 2


# ---- adversarial limits (rejected before parsing) ---------------------------


def test_depth_bomb_rejected() -> None:
    with pytest.raises(ProtocolError):
        extract_vars(_script("var a = " + "[" * 50 + "]" * 50 + ";"))


def test_huge_array_rejected() -> None:
    payload = "var a = [" + "1," * (MAX_ARRAY_ITEMS + 50) + "0];"
    with pytest.raises(ProtocolError):
        extract_vars(_script(payload))


def test_oversized_page_rejected() -> None:
    with pytest.raises(ProtocolError, match="too large"):
        extract_vars("a" * (MAX_INPUT_BYTES + 1))


def test_nul_bytes_rejected() -> None:
    with pytest.raises(ProtocolError, match="NUL"):
        extract_vars(_script("var a = 1;\x00"))


def test_identifier_too_long_rejected() -> None:
    with pytest.raises(ProtocolError):
        extract_vars(_script(f"var {'a' * 65} = 1;"))


# ---- purity -----------------------------------------------------------------


def test_input_not_mutated_and_fresh_result() -> None:
    html = _script("var a = {k:[1,2]};")
    first = extract_vars(html)
    second = extract_vars(html)
    assert first == second
    assert first is not second
    first["a"]["k"].append(99)
    assert extract_vars(html)["a"]["k"] == [1, 2]
