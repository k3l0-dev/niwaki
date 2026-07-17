"""Fabric access — Synchronous Ethernet interface policies (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/02_fabric-access/test_012_synce.py -m integration -s

The SyncE shelf: the SyncE interface policy across the admin-state x QL-option-type
cartesian (with the selection-config and SSM booleans swept both ways), and the
global SyncE instance policy across the admin-state x node-QL-option x
transmit-DNU cartesian. Values are illustrative and cover the SDK surface, not a
real timing plan.

# COVERAGE GAPS (managed-tag children reachable only via .mo(), mark parent
# extMngdBy=msc, deliberately not configured): external_tag_instance /
# tag_instance on synceEthIfPol.

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

ADMIN_STATES = ("disabled", "enabled")
IF_QL_OPTIONS = ("none", "op1", "op2g1", "op2g2")
NODE_QL_OPTIONS = ("op1", "op2g1", "op2g2")
DNU = (True, False)
# A quality-level value per option family — required by the APIC once a QL option
# type other than ``none`` is configured on the interface policy.
QL_FOR_OPTION = {
    "op1": "fsync-ql-o1-prc",
    "op2g1": "fsync-ql-o2-g1-prs",
    "op2g2": "fsync-ql-o2-g2-prs",
}
# The APIC accepts exactly one QL specification mode: (exact) OR (low-high) OR
# (high). ``test_synce_interface`` covers ``exact``; this table drives the other
# two modes as separate policies. (highest, lowest) per option family.
QL_HIGH_LOW = {
    "op1": ("fsync-ql-o1-prc", "fsync-ql-o1-sec"),
    "op2g1": ("fsync-ql-o2-g1-prs", "fsync-ql-o2-g1-st3"),
    "op2g2": ("fsync-ql-o2-g2-prs", "fsync-ql-o2-g2-st3"),
}
QL_MODES = ("lowhigh", "high")


def _common(obj: Cursor) -> None:
    """Attach the universal children (annotation, tag, RBAC domain marker)."""
    obj.mo(tagAnnotation, key="orchestrator", value="niwaki-it")
    obj.mo(tagTag, key="lifecycle", value="integration")
    obj.mo(aaaRbacAnnotation, domain="all")


def _if_name(admin: str, qlopt: str) -> str:
    return f"niwaki-it-synce-if-{admin}-{qlopt}"


def _dnu_slug(dnu: bool) -> str:
    return "dnu" if dnu else "nodnu"


def _inst_name(admin: str, qlopt: str, dnu: bool) -> str:
    return f"niwaki-it-synce-inst-{admin}-{qlopt}-{_dnu_slug(dnu)}"


def _ql_mode_name(qlopt: str, mode: str) -> str:
    return f"niwaki-it-synce-ql-{qlopt}-{mode}"


def test_synce_interface(live_aci: Niwaki) -> None:
    """SyncE interface policy: admin x QL-option, booleans swept both ways."""
    fab = infra()
    idx = 0
    for admin in ADMIN_STATES:
        for qlopt in IF_QL_OPTIONS:
            ql_value = QL_FOR_OPTION.get(qlopt)
            pol = fab.synce_interface_policy(
                _if_name(admin, qlopt),
                admin_state=admin,
                qloptype=qlopt,
                quality_receive_exact_ql_value=ql_value,
                quality_transmit_exact_ql_value=ql_value,
                selection_configuration=bool(idx & 1),
                ssm_configuration_enable_disable=bool(idx & 2),
                source_priority_1_254_default100=100 + idx,
                wait_to_restore_time=idx % 12,
                description=f"SyncE admin/QL-option matrix - admin {admin}, QL {qlopt}.",
            )
            _common(pol)
            idx += 1
    fab.push(live_aci)


def test_synce_instance(live_aci: Niwaki) -> None:
    """SyncE instance policy: admin x node-QL-option x transmit-DNU cartesian."""
    fab = infra()
    for admin in ADMIN_STATES:
        for qlopt in NODE_QL_OPTIONS:
            for dnu in DNU:
                inst = fab.synce_policy(
                    _inst_name(admin, qlopt, dnu),
                    admin_state=admin,
                    ql_option_type_node=qlopt,
                    transmit_dnu_on_lag_members=dnu,
                    description=f"SyncE instance matrix - admin {admin}, QL {qlopt}, "
                    f"DNU {_dnu_slug(dnu)}.",
                )
                _common(inst)
    fab.push(live_aci)


def test_synce_interface_ql_modes(live_aci: Niwaki) -> None:
    """SyncE interface QL specification modes: low-high and high-only.

    The APIC accepts exactly one of (exact) / (low-high) / (high). ``exact`` is
    covered by ``test_synce_interface``; this factors the other two mutually
    exclusive modes into their own policies, per QL option family.
    """
    fab = infra()
    for qlopt in NODE_QL_OPTIONS:
        highest, lowest = QL_HIGH_LOW[qlopt]
        low_high = fab.synce_interface_policy(
            _ql_mode_name(qlopt, "lowhigh"),
            admin_state="enabled",
            qloptype=qlopt,
            quality_receive_highest_ql_value=highest,
            quality_receive_lowest_ql_value=lowest,
            qltxhval=highest,
            quality_transmit_lowest_ql_value=lowest,
            description=f"SyncE QL-mode - {qlopt} low-high bounds.",
        )
        _common(low_high)
        high_only = fab.synce_interface_policy(
            _ql_mode_name(qlopt, "high"),
            admin_state="enabled",
            qloptype=qlopt,
            quality_receive_highest_ql_value=highest,
            qltxhval=highest,
            description=f"SyncE QL-mode - {qlopt} highest only.",
        )
        _common(high_only)
    fab.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    if_dns = [
        f"uni/infra/synceEthIfP-{_if_name(a, q)}" for a in ADMIN_STATES for q in IF_QL_OPTIONS
    ]
    ql_mode_dns = [
        f"uni/infra/synceEthIfP-{_ql_mode_name(q, m)}" for q in NODE_QL_OPTIONS for m in QL_MODES
    ]
    inst_dns = [
        f"uni/infra/synceInstP-{_inst_name(a, q, d)}"
        for a in ADMIN_STATES
        for q in NODE_QL_OPTIONS
        for d in DNU
    ]
    for dn in (*if_dns, *ql_mode_dns, *inst_dns):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
