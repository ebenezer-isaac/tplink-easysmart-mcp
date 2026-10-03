"""F6 — confirm_write string coercion defeats the gate at the real tool boundary.

``core.write_gate.check_write_gate`` is written to require the *boolean* ``True``
("``confirm_write`` must be the boolean ``True``; truthy strings do not count";
master-plan non-negotiable #4). But the FastMCP tool parameter is typed
``confirm_write: bool``, so pydantic coerces a JSON string ``"true"`` / ``"1"`` /
``"yes"`` to ``True`` BEFORE the gate ever sees it — ``confirm_write is not True``
then passes and a mutating ``*.cgi`` POST leaves the process.

This is exercised through the real ``mcp.call_tool`` boundary (not a direct op
call), and disproves the claim's "no mutating cgi request leaves the process
unless ... confirm_write=True": a bare string suffices. (Falsey strings still
refuse, so the gap only accepts truthy-but-non-boolean values.)
"""

from __future__ import annotations

import pytest

from tests.switch_fakes import StatefulSwitch, build_mcp, call, write_backend

POE_CGI = "/poe_port_config.cgi"


@pytest.mark.parametrize("truthy", ["true", "1", "yes"])
async def test_truthy_string_confirm_write_lets_the_write_through(tmp_path, truthy) -> None:
    switch = StatefulSwitch()
    backend, _ = write_backend(tmp_path, switch, port_map="cam1=1")
    mcp = build_mcp(backend)

    env = await call(
        mcp, "switch_set_poe", {"port": 1, "enabled": False, "confirm_write": truthy}
    )

    # CLAIMED INVARIANT: a mutating cgi request leaves the process only on the BOOLEAN
    # confirm_write=True ("truthy strings do not count", write_gate; master-plan #4).
    # FAILS today: FastMCP coerces the string to True before the gate sees it.
    assert env["success"] is False, f"string confirm_write={truthy!r} must be refused"
    assert switch.count("POST", POE_CGI) == 0, "no mutating cgi request may leave the process"


@pytest.mark.parametrize("falsey", ["false", "0"])
async def test_falsey_string_is_still_refused(tmp_path, falsey) -> None:
    # Control: falsey strings DO refuse, so the gap is specifically truthy-string bypass.
    switch = StatefulSwitch()
    backend, _ = write_backend(tmp_path, switch, port_map="cam1=1")
    mcp = build_mcp(backend)

    env = await call(
        mcp, "switch_set_poe", {"port": 1, "enabled": False, "confirm_write": falsey}
    )
    assert env["success"] is False
    assert env["error"]["code"] == "WRITE_REFUSED"
    assert switch.count("POST", POE_CGI) == 0
