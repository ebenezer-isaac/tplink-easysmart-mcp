"""R2 / secrets — the login RequestPlan carries the cleartext password in its repr.

The claim states the password "never appear[s] in logs, repr, exceptions or
envelopes". ``forms.build_login_form`` returns a frozen ``RequestPlan`` whose
``fields`` tuple holds ``("password", <cleartext>)``. ``RequestPlan`` is an
ordinary dataclass, so its auto-generated ``repr``/``str`` render every field —
including the password — verbatim. ``plan_redacted`` exists but is applied only on
the dry-run/logging path; the raw plan returned by ``build_login_form`` is the
object held while a login is in flight.

This is a latent leak: no current code path repr()s the login plan, so the
likelihood is low — but the claim's "never ... in repr" is literally false, and
any future ``log.debug("plan=%r", plan)`` or an exception that embeds the plan
would write the admin password to a log. The tests assert the invariant the claim
promises and fail today. No real device is contacted.
"""

from __future__ import annotations

from tplink_easysmart_mcp.switch.forms import build_login_form, plan_redacted
from tplink_easysmart_mcp.switch.models import LoginMode

# A password exercising urlencode-significant characters the config permits
# (6-16 chars, no spaces); '&', '=', '%' are all legal in EASYSMART_PASSWORD.
SECRET = "Pw1&=%x9"


def test_login_plan_repr_does_not_leak_password() -> None:
    plan = build_login_form("admin", SECRET, LoginMode.NORMAL)
    assert SECRET not in repr(plan)


def test_login_plan_str_does_not_leak_password() -> None:
    plan = build_login_form("admin", SECRET, LoginMode.NORMAL)
    assert SECRET not in str(plan)


def test_redacted_plan_repr_is_the_safe_shape() -> None:
    # Control: the redacted plan (what logging/dry-run should use) does not leak.
    plan = plan_redacted(build_login_form("admin", SECRET, LoginMode.NORMAL))
    assert SECRET not in repr(plan)
