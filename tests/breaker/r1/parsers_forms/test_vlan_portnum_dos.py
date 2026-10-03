"""F2 - `parse_vlans` does O(portNum) work on an unbounded declared port count.

The port-count pages (PortSettingRpm/PortStatisticsRpm) are protected from a
hostile "huge declared port count" because their per-port arrays must be at least
`max_port_num` long, and `jsvars` caps any array at 4,096 items. `parse_vlans`
has no such protection: `portNum` is a *scalar* int, so it escapes the array cap,
and `_ports_from_mask(mask, port_count)` iterates `range(1, port_count + 1)` once
per VLAN. A crafted VLAN page (the switch speaks plain HTTP with no TLS, so a LAN
host can inject one) that declares a large `portNum` while keeping the member
arrays tiny forces unbounded CPU on the single-threaded client and returns a
nonsense model.

Measured: `portNum = 5_000_000` took ~108 s and returned `port_count = 5_000_000`
(a 16-port switch). Larger values scale linearly, so the bound is effectively
"until a human kills the process".

The claim says the parsers "turn every page into a correct pydantic model and
never raise on hostile input (... 10,000 declared ports)". A port count far above
any real switch is neither a correct model nor a bounded operation.
"""

from __future__ import annotations

import re
import time

import pytest

from tplink_easysmart_mcp.switch import parsers
from tplink_easysmart_mcp.switch.errors import ProtocolError

from .conftest import load


def _with_portnum(html: str, value: int) -> str:
    out = re.sub(r"portNum:\s*\d+", f"portNum:{value}", html, count=1)
    assert f"portNum:{value}" in out
    return out


def test_absurd_portnum_is_rejected() -> None:
    """A declared port count far above any real switch must be rejected, not accepted.

    The jsvars layer bounds declared array sizes to 4,096 for exactly this reason;
    `portNum` bypasses that bound. A hardened parser raises ProtocolError. The
    current parser returns a VlanTable with port_count = 100_000 (fail-open).
    """
    html = _with_portnum(load("vlan_8021q.html"), 100_000)
    with pytest.raises(ProtocolError):
        parsers.parse_vlans(html)


def test_portnum_work_is_bounded_in_time() -> None:
    """Parsing a crafted VLAN page must not scale with the declared port count.

    portNum=1_000_000 measures ~5 s here (and 5_000_000 ~108 s); a correct parser
    is sub-second regardless of `portNum`. The assertion fails today, demonstrating
    the DoS. The cost is super-linear because `1 << (p - 1)` builds a bignum whose
    size grows with the declared port count.
    """
    html = _with_portnum(load("vlan_8021q.html"), 1_000_000)
    start = time.perf_counter()
    parsers.parse_vlans(html)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"parse_vlans took {elapsed:.1f}s for a crafted portNum (DoS)"
