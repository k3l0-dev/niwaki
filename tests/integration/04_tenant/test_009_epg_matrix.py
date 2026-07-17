"""Tenant — EPG matrix across application profiles, combination-exhaustive (non-prod).

Run:
    uv run pytest tests/integration/04_tenant/test_009_epg_matrix.py -m integration -s

An application EPG (``fvAEPg``) per combination of QoS class (level1..level6 +
unspecified) x provider-label match criteria (All / AtleastOne / AtmostOne /
None) x enforcement preference x preferred-group membership — 112 EPGs — spread
across six application profiles and two test functions, with flood-on-encap,
forwarding controls and shutdown covered per value. Each EPG binds the full
resolvable relation set (bridge domain, custom QoS, data-plane policing, QoS
requirement, trust control, monitoring) and carries a shared subnet.

Values are illustrative. This file owns tenant ``niwaki-it-epg``; ``wipe``
(operator-only) deletes it.
"""

from __future__ import annotations

import contextlib
import itertools

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

TN = "niwaki-it-epg"
TN_DESC = "EPG QoS/match/enforcement matrix across application profiles"
VRF = "niwaki-it-epg-vrf"
BD = "niwaki-it-epg-bd"
APPS = [f"niwaki-it-app-{c}" for c in "abcdef"]

QOS = ["level1", "level2", "level3", "level4", "level5", "level6", "unspecified"]
MATCH = ["All", "AtleastOne", "AtmostOne", "None"]
ENFORCE = ["enforced", "unenforced"]
PREFERRED = ["include", "exclude"]
COMBOS = list(itertools.product(QOS, MATCH, ENFORCE, PREFERRED))  # 112


def _foundation(tn):  # type: ignore[no-untyped-def]
    """Declare the VRF, a flood BD, the app profiles and the QoS/security policies."""
    tn.vrf(VRF, description="VRF backing the application EPGs.")
    tn.bd(
        BD,
        description="Flood BD backing the EPG matrix (allows flood-on-encap).",
        unicast_routing=True,
        unknown_mac_unicast_action="flood",
    ).bind(vrf=VRF)
    tn.custom_qos_policy("niwaki-it-cust-qos", description="Custom QoS policy bound onto the EPG.")
    tn.dpp_policy(
        "niwaki-it-dpp-1r2c",
        description="Single-rate two-colour policer.",
        admin_st="enabled",
        type="1R2C",
        rate=1000000,
        rate_unit="kilo",
        burst=1500,
        burst_unit="kilo",
    )
    tn.dpp_policy(
        "niwaki-it-dpp-2r3c",
        description="Two-rate three-colour policer.",
        admin_st="enabled",
        type="2R3C",
        rate=2000000,
        rate_unit="kilo",
        peak_rate=4000000,
        peak_rate_unit="kilo",
    )
    tn.qos_requirement(
        "niwaki-it-qos-req", description="QoS requirement bound onto the EPG."
    ).ingress_dpp("niwaki-it-dpp-1r2c").egress_dpp("niwaki-it-dpp-2r3c")
    tn.trust_control_policy(
        "niwaki-it-trust",
        description="First-hop trust-control policy bound onto the EPG.",
        trust_arp=True,
        trust_nd=True,
    )
    tn.monitoring_policy("niwaki-it-epg-mon", description="Monitoring policy bound onto the EPG.")
    return [
        tn.app(name, description="Application profile of the EPG QoS/match matrix.")
        for name in APPS
    ]


def _emit(tn, apps, combos, offset):  # type: ignore[no-untyped-def]
    """Build one EPG per combination in this slice."""
    for i, (qos, match, enforce, preferred) in enumerate(combos):
        idx = offset + i
        # flood-on-encap only pairs with preferred-group exclude.
        flood = "enabled" if (preferred == "exclude" and idx % 2 == 0) else "disabled"
        apps[idx % len(apps)].epg(
            f"niwaki-it-epg-{idx:03d}",
            description=f"EPG: QoS {qos}, match {match}, {enforce}, {preferred}.",
            qos_class=qos,
            provider_label_match_criteria=match,
            policy_control_enforcement=enforce,
            preferred_group_member=preferred,
            flood_on_encap=flood,
            forwarding_control_bits="proxy-arp" if idx % 2 else "",
            shutdown=bool(idx % 5 == 0),
        ).bind(
            bd=BD,
            custom_qos_policy="niwaki-it-cust-qos",
            dpp_policy="niwaki-it-dpp-1r2c",
            qos_requirement="niwaki-it-qos-req",
            trust_control_policy="niwaki-it-trust",
            monitoring_policy="niwaki-it-epg-mon",
        ).subnet(
            f"10.{130 + idx // 4}.{idx % 4}.1/24",
            description="Shared services subnet on the EPG.",
            scope="private,shared",
        )


def test_epg_matrix_slice_a(live_aci: Niwaki) -> None:
    """First half of the EPG matrix."""
    tn = tenant(TN, description=TN_DESC)
    apps = _foundation(tn)
    _emit(tn, apps, COMBOS[0:56], 0)
    tn.push(live_aci)


def test_epg_matrix_slice_b(live_aci: Niwaki) -> None:
    """Second half of the EPG matrix."""
    tn = tenant(TN, description=TN_DESC)
    apps = _foundation(tn)
    _emit(tn, apps, COMBOS[56:112], 56)
    tn.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (f"uni/tn-{TN}",):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
