"""Fabric access — L2-interface and STP interface policies (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/02_fabric-access/test_007_l2_stp.py -m integration -s

The L2 shelf: the L2 interface policy across the full QinQ x VEPA x VLAN-scope
cartesian, and the STP interface policy across its control-flag combinations
(unspecified, BPDU guard, BPDU filter, both). One object per valid combination.
Values are illustrative and cover the SDK surface, not a real L2 plan.

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

QINQ = ("corePort", "disabled", "doubleQtagPort", "edgePort")
VEPA = ("disabled", "enabled")
VLAN_SCOPE = ("global", "portlocal")
# STP control-flag combinations (StpIfControl).
STP_COMBOS: tuple[tuple[str, str], ...] = (
    ("unspecified", "unspecified"),
    ("guard", "bpdu-guard"),
    ("filter", "bpdu-filter"),
    ("both", "bpdu-guard,bpdu-filter"),
)


def _common(obj: Cursor) -> None:
    """Attach the universal children (annotation, tag, RBAC domain marker)."""
    obj.mo(tagAnnotation, key="orchestrator", value="niwaki-it")
    obj.mo(tagTag, key="lifecycle", value="integration")
    obj.mo(aaaRbacAnnotation, domain="all")


def _l2_name(qinq: str, vepa: str, scope: str) -> str:
    return f"niwaki-it-l2if-{qinq.lower()}-{vepa}-{scope}"


def _stp_name(slug: str) -> str:
    return f"niwaki-it-stp-{slug}"


def test_l2_interface(live_aci: Niwaki) -> None:
    """L2 interface policy: QinQ x VEPA x VLAN-scope cartesian."""
    fab = infra()
    for qinq in QINQ:
        for vepa in VEPA:
            for scope in VLAN_SCOPE:
                pol = fab.l2_interface_policy(
                    _l2_name(qinq, vepa, scope),
                    dot1q_tunnel_policy_configuration=qinq,
                    vepa_policy_configuration=vepa,
                    vlan_scope=scope,
                    description=f"L2 QinQ/VEPA/scope matrix - {qinq}, {vepa}, {scope}.",
                )
                _common(pol)
    fab.push(live_aci)


def test_stp_interface(live_aci: Niwaki) -> None:
    """STP interface policy across its control-flag combinations."""
    fab = infra()
    for slug, controls in STP_COMBOS:
        pol = fab.stp_policy(
            _stp_name(slug),
            controls=controls,
            description=f"STP control-flag sweep - ({controls}).",
        )
        _common(pol)
    fab.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    l2_dns = [
        f"uni/infra/l2IfP-{_l2_name(q, v, s)}" for q in QINQ for v in VEPA for s in VLAN_SCOPE
    ]
    stp_dns = [f"uni/infra/ifPol-{_stp_name(slug)}" for slug, _ in STP_COMBOS]
    for dn in (*l2_dns, *stp_dns):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
