"""Fabric access — Power-over-Ethernet interface policies (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/02_fabric-access/test_009_poe.py -m integration -s

The PoE shelf: the PoE interface policy across the power-mode x policing-action x
port-priority cartesian, plus admin-state coverage and a sweep of the named
maximum-power budgets; and the global PoE instance policy across its power-control
combinations. Values are illustrative and cover the SDK surface, not a real power
plan.

# COVERAGE GAPS (curated child reachable in the schema but not via a
# maker/bind/verb — reported, never forced):
#   - bind:poeRsPoeEpg@poeIfPol — the PoE interface policy's relation to the EPG
#     of the powered device has no bind alias on PoeInterfacePolicyCursor.
# Managed-tag children (reachable only via .mo(), mark parent extMngdBy=msc,
# deliberately not configured): external_tag_instance / tag_instance on poeIfPol.

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

ADMIN_STATES = ("enabled", "disabled")
POWER_MODES = ("auto", "never", "static")
POLICE_ACTIONS = ("err-dis", "log", "none")
PRIORITIES = (True, False)
MAX_POWER = (15400, 30000, 4000, 60000, 7000)
# Power-control combinations (PoePwrCtrl flags): empty and the single flag.
PWR_CTRL: tuple[tuple[str, str], ...] = (("none", ""), ("combined", "combined"))


def _common(obj: Cursor) -> None:
    """Attach the universal children (annotation, tag, RBAC domain marker)."""
    obj.mo(tagAnnotation, key="orchestrator", value="niwaki-it")
    obj.mo(tagTag, key="lifecycle", value="integration")
    obj.mo(aaaRbacAnnotation, domain="all")


def _prio_slug(prio: bool) -> str:
    return "high" if prio else "low"


def _matrix_name(pm: str, pa: str, prio: bool) -> str:
    return f"niwaki-it-poe-{pm}-{pa}-{_prio_slug(prio)}"


def _admin_name(admin: str) -> str:
    return f"niwaki-it-poe-admin-{admin}"


def _maxpower_name(mp: int) -> str:
    return f"niwaki-it-poe-maxpower-{mp}"


def _inst_name(slug: str) -> str:
    return f"niwaki-it-poeinst-{slug}"


def test_poe_matrix(live_aci: Niwaki) -> None:
    """PoE interface policy: power-mode x policing-action x port-priority."""
    fab = infra()
    idx = 0
    for pm in POWER_MODES:
        for pa in POLICE_ACTIONS:
            for prio in PRIORITIES:
                pol = fab.poe_interface_policy(
                    _matrix_name(pm, pa, prio),
                    admin_state="enabled",
                    power_mode=pm,
                    policing_action=pa,
                    port_priority_high=prio,
                    maximum_power=MAX_POWER[idx % len(MAX_POWER)],
                    consumption=4000,
                    description=f"PoE mode/police/prio matrix - {pm}, {pa}, {_prio_slug(prio)}.",
                )
                _common(pol)
                idx += 1
    fab.push(live_aci)


def test_poe_admin_and_power(live_aci: Niwaki) -> None:
    """PoE admin-state coverage and the named maximum-power budget sweep."""
    fab = infra()
    for admin in ADMIN_STATES:
        pol = fab.poe_interface_policy(
            _admin_name(admin),
            admin_state=admin,
            power_mode="auto",
            description=f"PoE admin-state sweep - {admin}.",
        )
        _common(pol)
    for mp in MAX_POWER:
        pol = fab.poe_interface_policy(
            _maxpower_name(mp),
            admin_state="enabled",
            power_mode="static",
            maximum_power=mp,
            description=f"PoE max-power sweep - {mp} mW.",
        )
        _common(pol)
    fab.push(live_aci)


def test_poe_instance(live_aci: Niwaki) -> None:
    """Global PoE instance policy across power-control combinations."""
    fab = infra()
    for slug, ctrl in PWR_CTRL:
        inst = fab.poe_policy(
            _inst_name(slug),
            consumption_default=4000 if slug == "none" else 7000,
            power_control=ctrl or None,
            description=f"PoE instance power-control sweep - ({slug}).",
        )
        _common(inst)
    fab.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    if_names: list[str] = []
    if_names += [
        _matrix_name(pm, pa, prio)
        for pm in POWER_MODES
        for pa in POLICE_ACTIONS
        for prio in PRIORITIES
    ]
    if_names += [_admin_name(a) for a in ADMIN_STATES]
    if_names += [_maxpower_name(mp) for mp in MAX_POWER]
    if_dns = [f"uni/infra/poeIfP-{n}" for n in if_names]
    inst_dns = [f"uni/infra/poeInstP-{_inst_name(slug)}" for slug, _ in PWR_CTRL]
    for dn in (*if_dns, *inst_dns):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
