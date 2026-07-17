"""Fabric access — storm-control interface policies (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/02_fabric-access/test_008_storm_control.py -m integration -s

The storm-control shelf: percentage-rate policies across the action x packet-type
cartesian (with a spread of rate/burst values), plus packets-per-second policies
carrying per-type pps rate/burst and the config-valid flag. Both storm-control
actions (drop, shutdown) and every packet type are exercised. Values are
illustrative and cover the SDK surface, not a real rate-limiting plan.

# COVERAGE GAPS (curated child in CHILD_MAP but reachable only via .mo(), and it
# marks the parent extMngdBy=msc — deliberately not configured):
#   - external_tag_instance (tagExtMngdInst) / tag_instance (tagInst) on stormctrlIfPol

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

ACTIONS = ("drop", "shutdown")
PACKET_TYPES = ("all", "bcast", "mcast", "unk-ucast")
# Percentage rate/burst pairs cycled across the percentage-based policies.
RATE_BURST = ((10.0, 20.0), (50.0, 60.0), (80.0, 90.0), (100.0, 100.0))
# Per-traffic-type percentage rate/burst — the broadcast / multicast /
# unknown-unicast rates each configured on their own policy.
PER_TYPE_KINDS = ("bcast", "mcast", "unkucast")


def _common(obj: Cursor) -> None:
    """Attach the universal children (annotation, tag, RBAC domain marker)."""
    obj.mo(tagAnnotation, key="orchestrator", value="niwaki-it")
    obj.mo(tagTag, key="lifecycle", value="integration")
    obj.mo(aaaRbacAnnotation, domain="all")


def _pct_name(action: str, ptype: str) -> str:
    return f"niwaki-it-storm-pct-{action}-{ptype}"


def _pps_name(action: str) -> str:
    return f"niwaki-it-storm-pps-{action}"


def _pertype_name(kind: str, action: str) -> str:
    return f"niwaki-it-storm-{kind}-{action}"


def test_storm_percentage(live_aci: Niwaki) -> None:
    """Percentage-rate storm control: action x packet-type cartesian."""
    fab = infra()
    for a_idx, action in enumerate(ACTIONS):
        for p_idx, ptype in enumerate(PACKET_TYPES):
            rate, burst = RATE_BURST[(a_idx + p_idx) % len(RATE_BURST)]
            pol = fab.storm_control_policy(
                _pct_name(action, ptype),
                traffic_rate=rate,
                burst_rate=burst,
                packet_type=ptype,
                storm_ctrl_action=action,
                storm_ctrl_soak_inst_count=3,
                is_uc_mc_bc_storm_pkt_cfg_valid="Invalid",
                description=f"Storm-control percentage matrix - {ptype}, action {action}.",
            )
            _common(pol)
    fab.push(live_aci)


def test_storm_pps(live_aci: Niwaki) -> None:
    """Packets-per-second storm control with per-type rate/burst, both actions."""
    fab = infra()
    for action in ACTIONS:
        pol = fab.storm_control_policy(
            _pps_name(action),
            is_uc_mc_bc_storm_pkt_cfg_valid="Valid",
            bc_rate_pps=10000,
            broadcast_max_burst_size=12000,
            mc_rate_pps=8000,
            multicast_max_burst_size=9000,
            uuc_rate_pps=6000,
            unknown_unicast_max_burst_size=7000,
            storm_ctrl_action=action,
            storm_ctrl_soak_inst_count=5,
            description=f"Storm-control per-type pps rate/burst - action {action}.",
        )
        _common(pol)
    fab.push(live_aci)


def test_storm_per_type_percentage(live_aci: Niwaki) -> None:
    """Per-traffic-type percentage storm control (broadcast / multicast / unk-ucast).

    The per-type rate fields are an alternative to the generic rate: each traffic
    type gets its own policy so all three per-type rate/burst fields are exercised,
    across both actions. The APIC only honours the per-type rates when
    ``is_uc_mc_bc_storm_pkt_cfg_valid`` is ``Valid`` — otherwise they are silently
    reset to their defaults.
    """
    fab = infra()
    for action in ACTIONS:
        bc = fab.storm_control_policy(
            _pertype_name("bcast", action),
            broadcast_traffic_rate=50.0,
            bc_burst_rate=60.0,
            storm_ctrl_action=action,
            is_uc_mc_bc_storm_pkt_cfg_valid="Valid",
            description=f"Storm-control per-type percentage - broadcast, action {action}.",
        )
        _common(bc)
        mc = fab.storm_control_policy(
            _pertype_name("mcast", action),
            multicast_traffic_rate=40.0,
            mc_burst_rate=50.0,
            storm_ctrl_action=action,
            is_uc_mc_bc_storm_pkt_cfg_valid="Valid",
            description=f"Storm-control per-type percentage - multicast, action {action}.",
        )
        _common(mc)
        uuc = fab.storm_control_policy(
            _pertype_name("unkucast", action),
            unknown_unicast_traffic_rate=30.0,
            uuc_burst_rate=40.0,
            storm_ctrl_action=action,
            is_uc_mc_bc_storm_pkt_cfg_valid="Valid",
            description=f"Storm-control per-type percentage - unknown-unicast, action {action}.",
        )
        _common(uuc)
    fab.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    pct_dns = [f"uni/infra/stormctrlifp-{_pct_name(a, p)}" for a in ACTIONS for p in PACKET_TYPES]
    pps_dns = [f"uni/infra/stormctrlifp-{_pps_name(a)}" for a in ACTIONS]
    pertype_dns = [
        f"uni/infra/stormctrlifp-{_pertype_name(k, a)}" for k in PER_TYPE_KINDS for a in ACTIONS
    ]
    for dn in (*pct_dns, *pps_dns, *pertype_dns):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
