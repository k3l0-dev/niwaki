"""Fabric access — CDP / LLDP / MCP interface policies (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/02_fabric-access/test_004_cdp_lldp_mcp.py -m integration -s

The link-layer discovery and mis-cabling shelf a leaf/spine access policy group
draws from. This file sweeps the full combination space of the three policies:
CDP across both admin states; LLDP across the receive x transmit x DCBX-version
cartesian; MCP across the admin x mode x per-VLAN-PDU cartesian (with grace-period
variation). One object per valid combination — this is a coverage sweep of the SDK
surface, not a production plan.

# COVERAGE GAPS (curated child in CHILD_MAP but reachable only via .mo(), and it
# marks the parent extMngdBy=msc + spawns shadow annotations — deliberately not
# configured):
#   - external_tag_instance (tagExtMngdInst) on cdpIfPol / lldpIfPol
#   - tag_instance (tagInst) on cdpIfPol / lldpIfPol

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
DCBX_VERSIONS = ("CEE", "IEEE")
MCP_MODES = ("on", "off")


def _common(obj: Cursor) -> None:
    """Attach the universal children carried by every ACI class.

    ``tagAnnotation`` (key/value), ``tagTag`` (lifecycle tag) and
    ``aaaRbacAnnotation`` (RBAC domain marker) have no curated maker, so ``.mo()``
    is the sanctioned path. The security domain ``all`` exists by default.
    """
    obj.mo(tagAnnotation, key="orchestrator", value="niwaki-it")
    obj.mo(tagTag, key="lifecycle", value="integration")
    obj.mo(aaaRbacAnnotation, domain="all")


def _cdp_name(admin: str) -> str:
    return f"niwaki-it-cdp-{admin}"


def _lldp_name(rx: str, tx: str, dcbx: str) -> str:
    return f"niwaki-it-lldp-{rx}-{tx}-{dcbx.lower()}"


def _mcp_name(admin: str, mode: str, pdu: str) -> str:
    return f"niwaki-it-mcp-{admin}-{mode}-{pdu}"


def test_cdp(live_aci: Niwaki) -> None:
    """CDP interface policy across both admin states."""
    fab = infra()
    for admin in ADMIN_STATES:
        cdp = fab.cdp_policy(
            _cdp_name(admin),
            admin_state=admin,
            description=f"CDP admin-state sweep - {admin}.",
        )
        _common(cdp)
    fab.push(live_aci)


def test_lldp(live_aci: Niwaki) -> None:
    """LLDP interface policy: receive x transmit x DCBX-version cartesian."""
    fab = infra()
    for rx in ADMIN_STATES:
        for tx in ADMIN_STATES:
            for dcbx in DCBX_VERSIONS:
                lldp = fab.lldp_policy(
                    _lldp_name(rx, tx, dcbx),
                    receive_state=rx,
                    transmit_state=tx,
                    dcbxp_version=dcbx,
                    description=f"LLDP rx/tx/DCBX matrix - rx {rx}, tx {tx}, DCBX {dcbx}.",
                )
                _common(lldp)
    fab.push(live_aci)


def test_mcp(live_aci: Niwaki) -> None:
    """MisCabling Protocol: admin x mode x per-VLAN-PDU cartesian."""
    fab = infra()
    for admin in ADMIN_STATES:
        for mode in MCP_MODES:
            for pdu in MCP_MODES:
                mcp = fab.mcp_policy(
                    _mcp_name(admin, mode, pdu),
                    admin_state=admin,
                    mode=mode,
                    mcp_pdu_per_vlan=pdu,
                    grace_period=3 if mode == "on" else 5,
                    maximum_number_of_vlans=256,
                    description=f"MCP admin/mode/PDU matrix - {admin}, {mode}, {pdu}.",
                )
                _common(mcp)
    fab.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    dns: list[str] = []
    dns += [f"uni/infra/cdpIfP-{_cdp_name(a)}" for a in ADMIN_STATES]
    dns += [
        f"uni/infra/lldpIfP-{_lldp_name(rx, tx, d)}"
        for rx in ADMIN_STATES
        for tx in ADMIN_STATES
        for d in DCBX_VERSIONS
    ]
    dns += [
        f"uni/infra/mcpIfP-{_mcp_name(a, m, p)}"
        for a in ADMIN_STATES
        for m in MCP_MODES
        for p in MCP_MODES
    ]
    for dn in dns:
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
