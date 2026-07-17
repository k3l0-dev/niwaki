"""Tenant — endpoint security group matrix, combination-exhaustive (non-prod).

Run:
    uv run pytest tests/integration/04_tenant/test_014_esg.py -m integration -s

An endpoint security group (``fvESg``) per combination of QoS class
(level1..level6 + unspecified) x enforcement preference x provider-label match
criteria (All / AtleastOne / AtmostOne / None) x preferred-group membership —
112 ESGs — spread across four application profiles and two test functions, with
shutdown covered per value. Each ESG binds the mandatory VRF scope and a
custom-QoS policy. Selectors are covered on the first ESG of each slice: IP /
endpoint match expressions, an EPG selector, and tag selectors across every
value operator (equals / contains / regex).

Values are illustrative. The ESG monitoring-policy bind resolves to a no-op on
this class and the lif-ctx selector targets a service-graph object — neither is
exercised here. This file owns tenant ``niwaki-it-esg``; ``wipe`` (operator-only)
deletes it.
"""

from __future__ import annotations

import contextlib
import itertools

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

TN = "niwaki-it-esg"
TN_DESC = "ESG QoS/enforcement/match matrix plus selectors"
VRF = "niwaki-it-esg-vrf"
BD = "niwaki-it-esg-bd"
APPS = [f"niwaki-it-esg-app-{c}" for c in "abcd"]
EPG = "niwaki-it-esg-epg"
EPG_DN = f"uni/tn-{TN}/ap-{APPS[0]}/epg-{EPG}"

QOS = ["level1", "level2", "level3", "level4", "level5", "level6", "unspecified"]
ENFORCE = ["enforced", "unenforced"]
MATCH = ["All", "AtleastOne", "AtmostOne", "None"]
PREFERRED = ["include", "exclude"]
COMBOS = list(itertools.product(QOS, ENFORCE, MATCH, PREFERRED))  # 112


def _foundation(tn):  # type: ignore[no-untyped-def]
    """Declare the VRF, a BD, the app profiles and the referenced EPG."""
    tn.vrf(VRF, description="VRF the endpoint security groups live in.")
    tn.bd(BD, description="BD backing the referenced application EPG.", unicast_routing=True).bind(
        vrf=VRF
    )
    tn.custom_qos_policy("niwaki-it-esg-qos", description="Custom QoS policy bound onto the ESG.")
    apps = [
        tn.app(name, description="Application profile of the ESG QoS/enforcement matrix.")
        for name in APPS
    ]
    apps[0].epg(EPG, description="Application EPG referenced by the ESG epg-selector.").bind(bd=BD)
    return apps


def _selectors(esg):  # type: ignore[no-untyped-def]
    """Attach every selector kind to a representative ESG."""
    esg.ep_selector("ip=='10.33.1.7'", description="Endpoint selector matching a host address.")
    esg.ep_selector("ip=='10.33.2.0/24'", description="Endpoint selector matching a subnet.")
    esg.epg_selector(EPG_DN, description="EPG selector pulling in an application EPG.")
    esg.tag_selector(
        "environment",
        "production",
        description="Tag selector, exact match.",
        match_value_operator="equals",
    )
    esg.tag_selector(
        "tier",
        "web.*",
        description="Tag selector, regular-expression match.",
        match_value_operator="regex",
    )
    esg.tag_selector(
        "owner",
        "platform",
        description="Tag selector, substring match.",
        match_value_operator="contains",
    )


def _emit(tn, apps, combos, offset, *, selectors):  # type: ignore[no-untyped-def]
    """Build one ESG per combination in this slice; selectors on the first if asked.

    Tag selectors must be unique per VRF, so the selector set is attached to a
    single ESG across the whole tenant (the first slice only).
    """
    first = None
    for i, (qos, enforce, match, preferred) in enumerate(combos):
        idx = offset + i
        esg = (
            apps[idx % len(apps)]
            .esg(
                f"niwaki-it-esg-{idx:03d}",
                description=f"ESG: QoS {qos}, {enforce}, match {match}, {preferred}.",
                qos_class=qos,
                policy_control_enforcement=enforce,
                provider_label_match_criteria=match,
                preferred_group_member=preferred,
                shutdown=bool(idx % 5 == 0),
            )
            .bind(vrf=VRF, custom_qos_policy="niwaki-it-esg-qos")
        )
        if first is None:
            first = esg
    if selectors:
        assert first is not None
        _selectors(first)


def test_esg_matrix_slice_a(live_aci: Niwaki) -> None:
    """First half of the ESG matrix."""
    tn = tenant(TN, description=TN_DESC)
    apps = _foundation(tn)
    _emit(tn, apps, COMBOS[0:56], 0, selectors=True)
    tn.push(live_aci)


def test_esg_matrix_slice_b(live_aci: Niwaki) -> None:
    """Second half of the ESG matrix."""
    tn = tenant(TN, description=TN_DESC)
    apps = _foundation(tn)
    _emit(tn, apps, COMBOS[56:112], 56, selectors=False)
    tn.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (f"uni/tn-{TN}",):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
