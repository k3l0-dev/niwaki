"""Fabric access — Fibre-Channel interface policies (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/02_fabric-access/test_010_fibre_channel.py -m integration -s

The FC shelf: the FC interface policy across the port-mode x trunking-mode
cartesian plus independent sweeps of every port speed, auto-max-speed and
fill-pattern; and the global FC instance policy across its control-flag
combinations. Values are illustrative and cover the SDK surface, not a real SAN
plan.

This file owns only its niwaki-it-* policies; wipe(aci) removes them and is run by
hand (never by the suite).
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import Cursor, infra
from niwaki.exceptions import NotFoundError
from niwaki.models._generated.aaa.aaaRbacAnnotation import aaaRbacAnnotation
from niwaki.models._generated.tag.tagAnnotation import tagAnnotation
from niwaki.models._generated.tag.tagTag import tagTag

pytestmark = pytest.mark.integration

PORT_MODES = ("f", "np")
TRUNK_MODES = ("auto", "trunk-off", "trunk-on", "un-init")
FC_SPEEDS = ("16G", "32G", "4G", "8G", "auto", "unknown")
MAX_SPEEDS = ("16G", "2G", "32G", "4G", "8G")
FILL_PATTERNS = ("ARBFF", "IDLE")
# FC instance control-flag combinations (NwInstCtrl).
INST_CTRL: tuple[tuple[str, str], ...] = (
    ("none", ""),
    ("statefulha", "stateful-ha"),
    ("loadbalance", "load-balance"),
    ("both", "stateful-ha,load-balance"),
)


def _common(obj: Cursor) -> None:
    """Attach the universal children (annotation, tag, RBAC domain marker)."""
    obj.mo(tagAnnotation, key="orchestrator", value="niwaki-it")
    obj.mo(tagTag, key="lifecycle", value="integration")
    obj.mo(aaaRbacAnnotation, domain="all")


def _slug(value: str) -> str:
    return value.lower()


def _mode_name(pm: str, trunk: str) -> str:
    return f"niwaki-it-fc-{pm}-{trunk}"


def _speed_name(speed: str) -> str:
    return f"niwaki-it-fc-speed-{_slug(speed)}"


def _maxspeed_name(ms: str) -> str:
    return f"niwaki-it-fc-maxspeed-{_slug(ms)}"


def _fill_name(fill: str) -> str:
    return f"niwaki-it-fc-fill-{_slug(fill)}"


def _inst_name(slug: str) -> str:
    return f"niwaki-it-fcinst-{slug}"


def test_fc_mode_trunk(live_aci: Niwaki) -> None:
    """FC interface policy: port-mode x trunking-mode cartesian."""
    fab = infra()
    for pm in PORT_MODES:
        for trunk in TRUNK_MODES:
            pol = fab.fc_interface_policy(
                _mode_name(pm, trunk),
                port_mode_property_fnp=pm,
                trunking_mode=trunk,
                speed="auto",
                automaxspeed="32G",
                fill_pattern="IDLE",
                rx_bb_credit=64,
                description=f"FC port-mode/trunking matrix - {pm}-port, trunk {trunk}.",
            )
            _common(pol)
    fab.push(live_aci)


def test_fc_speeds(live_aci: Niwaki) -> None:
    """FC interface policy: one per port speed, one per auto-max-speed, per fill."""
    fab = infra()
    for speed in FC_SPEEDS:
        pol = fab.fc_interface_policy(
            _speed_name(speed),
            port_mode_property_fnp="f",
            trunking_mode="trunk-off",
            speed=speed,
            rx_bb_credit=32,
            description=f"FC speed sweep - {speed}.",
        )
        _common(pol)
    for ms in MAX_SPEEDS:
        pol = fab.fc_interface_policy(
            _maxspeed_name(ms),
            port_mode_property_fnp="f",
            automaxspeed=ms,
            speed="auto",
            description=f"FC auto-max-speed sweep - {ms}.",
        )
        _common(pol)
    for fill in FILL_PATTERNS:
        pol = fab.fc_interface_policy(
            _fill_name(fill),
            port_mode_property_fnp="np",
            fill_pattern=fill,
            speed="8G",
            description=f"FC fill-pattern sweep - {fill}.",
        )
        _common(pol)
    fab.push(live_aci)


def test_fc_instance(live_aci: Niwaki) -> None:
    """Global FC instance policy across its control-flag combinations."""
    fab = infra()
    for idx, (slug, ctrl) in enumerate(INST_CTRL):
        inst = fab.fc_instance_policy(
            _inst_name(slug),
            controls=ctrl or None,
            fip_keepalive_interval=(8, 4, 12, 8)[idx % 4],
            description=f"FC instance control-flag sweep - ({slug}).",
        )
        _common(inst)
    fab.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    if_names: list[str] = []
    if_names += [_mode_name(pm, t) for pm in PORT_MODES for t in TRUNK_MODES]
    if_names += [_speed_name(s) for s in FC_SPEEDS]
    if_names += [_maxspeed_name(ms) for ms in MAX_SPEEDS]
    if_names += [_fill_name(f) for f in FILL_PATTERNS]
    if_dns = [f"uni/infra/fcIfPol-{n}" for n in if_names]
    inst_dns = [f"uni/infra/fcinstpol-{_inst_name(slug)}" for slug, _ in INST_CTRL]
    for dn in (*if_dns, *inst_dns):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
