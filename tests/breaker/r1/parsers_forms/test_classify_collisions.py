"""F1 - a DATA page is misclassified as the LOGIN page.

`pages.classify` / `pages.is_login_page` decide LOGIN_PAGE by a bare substring
search for ``var logonInfo`` or ``action="/logon.cgi"`` over the *entire* body,
including quoted string values inside the first <script> block. So any
user-controlled string that is reflected into a data page -- a VLAN name, the
system description -- can contain one of those substrings and flip a perfectly
valid data page to LOGIN_PAGE, which makes the parser raise ``SessionExpired``
instead of returning the model.

The claim says the classifier "never misclassifies a data page as a login page
or vice versa". Each test asserts the correct outcome (DATA / the model parses);
they fail against the current substring classifier.
"""

from __future__ import annotations

from tplink_easysmart_mcp.switch import pages, parsers
from tplink_easysmart_mcp.switch.models import PageClass

from .conftest import load

# `var logonInfo` is 13 characters and fits inside the switch's 16-character
# VLAN-name / description limits, so it is reachable by anyone who can set those.
VAR_MARKER = "var logonInfo"
ACTION_MARKER = 'action="/logon.cgi"'


def test_vlan_name_containing_marker_is_not_a_login_page() -> None:
    """A VLAN named `var logonInfo` must still parse as the VLAN table."""
    html = load("vlan_8021q.html").replace("'cams'", f"'{VAR_MARKER}'")

    # The page still carries its real anchor variable, so it is DATA, not login.
    assert pages.classify(html, "qvlan_ds") is PageClass.DATA

    table = parsers.parse_vlans(html)
    assert VAR_MARKER in [v.name for v in table.vlans]


def test_system_description_containing_var_marker_parses() -> None:
    """A device description that contains the `var logonInfo` marker must parse."""
    html = load("system_info.html").replace("TL-SG1016PE 3.0", f"TL-SG1016PE {VAR_MARKER}")

    assert pages.classify(html, "info_ds") is PageClass.DATA

    info = parsers.parse_system_info(html)
    assert info.hardware is not None and VAR_MARKER in info.hardware


def test_vlan_name_containing_action_marker_parses() -> None:
    """The second login marker (`action="/logon.cgi"`) also must not flip a data page.

    The VLAN name is single-quoted in the page, so the marker's double quotes need
    no escaping and appear verbatim -- exactly what `is_login_page` scans for.
    """
    name = 'lan action="/logon.cgi"'
    html = load("vlan_8021q.html").replace("'cams'", repr(name))

    assert pages.classify(html, "qvlan_ds") is PageClass.DATA

    table = parsers.parse_vlans(html)
    assert name in [v.name for v in table.vlans]
