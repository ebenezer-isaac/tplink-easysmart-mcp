"""Write tools: ``switch_set_poe`` and ``switch_set_port`` (read-modify-write).

Both gates (``EASYSMART_ALLOW_WRITES`` and ``confirm_write``) are checked by the
server **before** either op is called, so nothing here touches the network until
the config-only refusals (unknown name, non-PoE port, protected port) have
passed. Each op then reads the live page, rebuilds the *full* per-port form via
``forms`` (changing only the admin state), submits, re-reads and verifies — a
mismatch is ``WRITE_VERIFY_FAILED`` with before/after, never a silent success.
Dry-run returns the exact redacted form and sends no mutating request; every op
runs inside ``session_scope(logout_after=True)`` so the switch is logged out
after success and after failure alike.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from .config import MAX_PORT, SwitchSettings
from .errors import NotPoePort, ProtectedPort, WriteVerifyFailed
from .forms import build_poe_port_form, build_port_setting_query
from .models import PoePort, PortState
from .tools_read import ResolvedPort, poe_view, port_view, resolve_port

if TYPE_CHECKING:
    from .backend import EasySmartSwitchBackend


class SetPoeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    port: int | str
    enabled: bool


class SetPortInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    port: int | str
    enabled: bool


def refuse_non_poe_config(settings: SwitchSettings, resolved: ResolvedPort) -> None:
    """Config-only PoE check (no network): the port must be in ``EASYSMART_POE_PORTS``."""
    if not settings.is_poe_port(resolved.port):
        raise NotPoePort(
            f"port {resolved.port} is not a PoE port (EASYSMART_POE_PORTS="
            f"{list(settings.poe_ports)})",
            port=resolved.port,
            poe_ports=list(settings.poe_ports),
        )


def refuse_protected(settings: SwitchSettings, resolved: ResolvedPort) -> None:
    if settings.is_protected(resolved.port):
        raise ProtectedPort(
            f"port {resolved.port} is protected (EASYSMART_PROTECTED_PORTS="
            f"{list(settings.protected_ports)}); refusing to change it",
            port=resolved.port,
            protected_ports=list(settings.protected_ports),
        )


def refuse_non_poe_live(
    settings: SwitchSettings, resolved: ResolvedPort, poe_port_num: int
) -> None:
    if resolved.port > poe_port_num:
        raise NotPoePort(
            f"port {resolved.port} is above the switch's live poe_port_num={poe_port_num}",
            port=resolved.port,
            poe_ports=list(settings.poe_ports),
            poe_port_num=poe_port_num,
        )


async def set_poe_op(
    backend: EasySmartSwitchBackend, *, port: int | str, enabled: bool
) -> dict[str, Any]:
    settings = backend.settings
    args = SetPoeInput(port=port, enabled=enabled)
    resolved = resolve_port(args.port, settings=settings, max_port=MAX_PORT)
    refuse_non_poe_config(settings, resolved)
    refuse_protected(settings, resolved)
    async with backend.client.session_scope(logout_after=True):
        snap = await backend.client.poe()
        backend.observe_poe_port_num(snap.poe_port_num)
        refuse_non_poe_live(settings, resolved, snap.poe_port_num)
        before = snap.ports[resolved.port - 1]
        before_view = poe_view(settings, before)
        if before.enabled == args.enabled:
            return _result(resolved, changed=False, before=before_view, after=before_view)
        plan = build_poe_port_form(before, args.enabled)
        sent = await backend.client.submit(plan)
        if sent is not None:  # dry-run: nothing was sent
            return _dry_run(resolved, before_view, sent.as_dict())
        after = (await backend.client.poe()).ports[resolved.port - 1]
        after_view = poe_view(settings, after)
        _verify_poe(before, after, args.enabled, before_view, after_view)
        return _result(resolved, changed=True, before=before_view, after=after_view)


async def set_port_op(
    backend: EasySmartSwitchBackend, *, port: int | str, enabled: bool
) -> dict[str, Any]:
    settings = backend.settings
    args = SetPortInput(port=port, enabled=enabled)
    resolved = resolve_port(args.port, settings=settings, max_port=MAX_PORT)
    refuse_protected(settings, resolved)
    async with backend.client.session_scope(logout_after=True):
        ports = await backend.client.ports()
        if resolved.port > len(ports):
            from .errors import InvalidPort

            raise InvalidPort(
                f"port {resolved.port} is above the switch's max_port_num={len(ports)}",
                port=resolved.port,
                max_port=len(ports),
            )
        before = ports[resolved.port - 1]
        before_view = port_view(settings, before)
        if before.enabled == args.enabled:
            return _result(resolved, changed=False, before=before_view, after=before_view)
        plan = build_port_setting_query(before, args.enabled)
        sent = await backend.client.submit(plan)
        if sent is not None:  # dry-run
            return _dry_run(resolved, before_view, sent.as_dict())
        after = (await backend.client.ports())[resolved.port - 1]
        after_view = port_view(settings, after)
        _verify_port(before, after, args.enabled, before_view, after_view)
        return _result(resolved, changed=True, before=before_view, after=after_view)


def _verify_poe(
    before: PoePort,
    after: PoePort,
    enabled: bool,
    before_view: dict[str, Any],
    after_view: dict[str, Any],
) -> None:
    if after.enabled != enabled:
        raise WriteVerifyFailed(
            f"port {after.number} PoE did not change to {'on' if enabled else 'off'}",
            before=before_view,
            after=after_view,
        )
    if (
        after.priority != before.priority
        or after.limit_kind != before.limit_kind
        or after.limit_w != before.limit_w
    ):
        raise WriteVerifyFailed(
            f"port {after.number}: the write clobbered the PoE priority or power limit",
            before=before_view,
            after=after_view,
        )


def _verify_port(
    before: PortState,
    after: PortState,
    enabled: bool,
    before_view: dict[str, Any],
    after_view: dict[str, Any],
) -> None:
    if after.enabled != enabled:
        raise WriteVerifyFailed(
            f"port {after.number} did not change to {'enabled' if enabled else 'disabled'}",
            before=before_view,
            after=after_view,
        )
    if after.speed_config != before.speed_config or after.fc_config != before.fc_config:
        raise WriteVerifyFailed(
            f"port {after.number}: the write clobbered the speed or flow-control setting",
            before=before_view,
            after=after_view,
        )


def _result(
    resolved: ResolvedPort, *, changed: bool, before: dict[str, Any], after: dict[str, Any]
) -> dict[str, Any]:
    return {
        "port": resolved.port,
        "name": resolved.name,
        "changed": changed,
        "before": before,
        "after": after,
    }


def _dry_run(
    resolved: ResolvedPort, before: dict[str, Any], planned: dict[str, str]
) -> dict[str, Any]:
    return {
        "dry_run": True,
        "port": resolved.port,
        "name": resolved.name,
        "before": before,
        "planned": planned,
    }
