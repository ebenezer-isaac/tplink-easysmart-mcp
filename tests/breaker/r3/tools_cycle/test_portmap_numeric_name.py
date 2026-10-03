"""F5 — a numeric PORT_MAP name shadows the literal port (wrong camera cycled).

``config._PORT_NAME = [A-Za-z0-9_.-]{1,32}`` accepts purely-numeric names, so
``EASYSMART_PORT_MAP="3=5"`` is a valid config (name "3" -> port 5). In
``resolve_port`` the name lookup runs BEFORE the "is it a digit string?" branch::

    if key in names:            # "3" -> port 5
        return ...
    if text.isascii() and text.isdigit():   # never reached for "3"
        return int(text)        # would have been port 3

So the same logical port the caller means resolves differently by TYPE alone:

    resolve_port("3") -> port 5   (the camera named "3")
    resolve_port(3)   -> port 3

An LLM routinely stringifies numbers, so ``switch_poe_cycle("3")`` can power-cycle
a DIFFERENT physical camera than ``switch_poe_cycle(3)`` while the envelope still
reports it acted on the requested value. The claim says an "ambiguous name" is
refused; this ambiguity instead resolves silently to the name.
"""

from __future__ import annotations

from tests.switch_fakes import StatefulSwitch, write_backend
from tplink_easysmart_mcp.core.tooling import run_tool
from tplink_easysmart_mcp.switch.config import MAX_PORT
from tplink_easysmart_mcp.switch.cycle import poe_cycle_op
from tplink_easysmart_mcp.switch.tools_read import resolve_port

POE_CGI = "/poe_port_config.cgi"


def test_numeric_name_string_resolves_to_a_different_port_than_the_int(tmp_path) -> None:
    switch = StatefulSwitch()
    backend, _ = write_backend(tmp_path, switch, port_map="3=5", protected_ports="16")

    as_string = resolve_port("3", settings=backend.settings, max_port=MAX_PORT)
    as_int = resolve_port(3, settings=backend.settings, max_port=MAX_PORT)

    # CLAIMED INVARIANT: the resolver "selects exactly one port" unambiguously; the same
    # logical value must not resolve to two different ports by type alone (or a numeric
    # name must be refused as ambiguous). FAILS today: "3" -> port 5, 3 -> port 3.
    assert as_string.port == as_int.port, (
        f'"3" resolved to port {as_string.port} but 3 resolved to port {as_int.port}'
    )


async def test_cycle_with_string_3_acts_on_port_5_not_port_3(tmp_path) -> None:
    switch = StatefulSwitch()
    backend, _ = write_backend(tmp_path, switch, port_map="3=5", protected_ports="16")

    env = await run_tool(
        "switch_poe_cycle", lambda: poe_cycle_op(backend, port_or_name="3", off_seconds=10)
    )

    # CLAIMED INVARIANT: asking for "3" must act on port 3 (the literal), not silently on
    # whatever port a camera happens to be NAMED "3". FAILS today: it cycles port 5.
    cycled_ports = {
        next(int(k[4:]) for k in b if k.startswith("sel_")) for b in switch.poe_write_bodies()
    }
    assert cycled_ports == {3}, f"cycling '3' drove physical ports {cycled_ports}, not port 3"
    assert env["data"]["port"] == 3
