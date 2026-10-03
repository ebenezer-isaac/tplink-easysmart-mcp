"""F3 / F4 - login-page probe false positives from naive substring matching.

`probe_login_page` decides `auth_variant` and `login_mode` with bare substring
searches over the whole body:

* `_auth_variant` -> ENCRYPTED if any ENCRYPTED_MARKER (e.g. ``securityEncode``)
  appears anywhere, including inside an HTML comment.
* `_login_mode` -> RESTORED_ACCOUNT if ``value="Confirm"`` appears anywhere,
  including a hidden static input the page only reveals (via JS) when errType==6.

The `account_restored` case was handled carefully (it matches ``var
account_restored=1`` to dodge the static `account_restored=0`/`=1` text), but the
`securityEncode`/`value="Confirm"` paths got no such care. On a plain,
normal-mode login page either marker flips the verdict and makes the client
refuse to log in (encrypted -> AUTH_VARIANT_UNSUPPORTED; restored -> the form
builder raises RestoredAccountMode).

The claim says the classifier "correctly distinguishes a plain login page, the
encrypted-login variant, [and] the restored-account/factory-reset page". These
tests assert the correct verdict on a normal page; they fail today.
"""

from __future__ import annotations

from tplink_easysmart_mcp.switch import pages
from tplink_easysmart_mcp.switch.models import AuthVariant, LoginMode

from .conftest import load


def test_encrypted_marker_in_comment_is_not_the_encrypted_variant() -> None:
    """A plain page that merely mentions `securityEncode` in a comment is plain_form."""
    html = load("login_page.html").replace(
        "</head>", "<!-- firmware note: securityEncode is NOT used here --></head>"
    )
    probe = pages.probe_login_page(html, None)
    assert probe.auth_variant is AuthVariant.PLAIN_FORM


def test_static_confirm_input_is_not_restored_account() -> None:
    """A normal (errType 0) page carrying a hidden `value="Confirm"` input is normal mode.

    Many TP-Link login templates ship the Confirm-password input in the static
    HTML and only reveal it via JS when errType==6. Keying on the substring
    misreads such a plain page as factory-reset mode and blocks login entirely.
    """
    html = load("login_page.html").replace(
        "</head>",
        '</head><input type="submit" id="confirmBtn" '
        'style="display:none" value="Confirm">',
    )
    probe = pages.probe_login_page(html, None)
    assert probe.err_type == 0
    assert probe.login_mode is LoginMode.NORMAL
