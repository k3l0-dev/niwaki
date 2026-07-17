"""External connectivity — external EPGs and subnets, full scope/aggregate matrix (non-prod).

Run:
    uv run pytest tests/integration/06_l3out/test_009_external_epgs.py -m integration -s

External EPGs classify outside prefixes. This file sweeps the ``l3extInstP``
attributes (preferred-group membership, policy-control enforcement, QoS class) one
EPG per combination, and the ``l3extSubnet`` scope flags across a broad set of
valid combinations (respecting the rule that a shared-security subnet must also
carry import-security), plus the route-aggregation flags on default routes (one
EPG per aggregate flavour, since an EPG holds a single 0.0.0.0/0). It also covers
the full contract-label set and the provide / consume / intra-EPG verbs.

One VRF backs the L3Out. Values are illustrative. ``wipe(aci)`` is operator-only.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.design._cursor import Cursor
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

TN = "niwaki-it-l3out"
POOL = "niwaki-it-l3v"
L3DOM = "niwaki-it-l3d"
L3OUT = "niwaki-it-l3o-extepg"
VRF = "niwaki-it-l3o-extepg-vrf"

# Valid l3extSubnet scope combinations (shared-security implies import-security).
SCOPES = [
    "import-security",
    "import-security,shared-security",
    "export-rtctrl",
    "import-rtctrl",
    "export-rtctrl,import-rtctrl",
    "export-rtctrl,shared-rtctrl",
    "import-security,shared-security,import-rtctrl,export-rtctrl,shared-rtctrl",
]
# aggregate flavours (each on its own default route in its own EPG).
AGGREGATES = [
    ("export-rtctrl", "export-rtctrl"),
    ("import-rtctrl", "import-rtctrl"),
    ("import-security,shared-security,shared-rtctrl", "shared-rtctrl"),
]
QOS = ["level1", "level2", "level3", "unspecified"]


def _scaffold(t: Cursor) -> None:
    t.infra().vlan_pool(POOL, "static").range(
        "vlan-2600", "vlan-2699", allocation_mode="static", role="external"
    )
    t.l3_dom(L3DOM).bind(vlan_pool=POOL)


def test_subnet_scope_matrix(live_aci: Niwaki) -> None:
    """One subnet per valid scope combination, plus a route-control profile bind."""
    t = tenant(TN)
    _scaffold(t)
    t.vrf(VRF, description="VRF for the external-EPG L3Out.")
    t.route_control_profile("niwaki-it-epg-rc", type="global", description="Subnet route-control.")
    out = (
        t.l3out(
            L3OUT,
            description="External EPGs, subnet scope/aggregate matrix, labels and contract verbs.",
        )
        .bind(vrf=VRF)
        .bind(domain=L3DOM)
    )

    epg = out.external_epg("niwaki-it-scopes", description="External EPG holding the scope matrix.")
    for i, scope in enumerate(SCOPES):
        sub = epg.subnet(f"10.{i}.0.0/16", scope=scope, description=f"Subnet scope {scope}.")
        if "rtctrl" in scope:
            sub.bind(route_control_profile="niwaki-it-epg-rc")

    t.push(live_aci)


def test_aggregate_default_routes(live_aci: Niwaki) -> None:
    """One external EPG per aggregate flavour, each with an aggregated default route."""
    t = tenant(TN)
    _scaffold(t)
    t.vrf(VRF, description="VRF for the external-EPG L3Out.")
    out = (
        t.l3out(
            L3OUT,
            description="External EPGs, subnet scope/aggregate matrix, labels and contract verbs.",
        )
        .bind(vrf=VRF)
        .bind(domain=L3DOM)
    )

    for i, (scope, aggregate) in enumerate(AGGREGATES):
        epg = out.external_epg(f"niwaki-it-agg-{i}", description=f"Aggregate {aggregate}.")
        epg.subnet(
            "0.0.0.0/0",
            scope=scope,
            aggregate=aggregate,
            description=f"Default route, aggregate {aggregate}.",
        )

    t.push(live_aci)


def test_epg_attributes_and_labels(live_aci: Niwaki) -> None:
    """One EPG per (preferred-group x enforcement x QoS) mix, plus labels and verbs."""
    t = tenant(TN)
    _scaffold(t)
    t.vrf(VRF, description="VRF for the external-EPG L3Out.")
    t.contract("niwaki-it-epg-ctr", scope="context", description="Contract for EPG verbs.")
    out = (
        t.l3out(
            L3OUT,
            description="External EPGs, subnet scope/aggregate matrix, labels and contract verbs.",
        )
        .bind(vrf=VRF)
        .bind(domain=L3DOM)
    )

    n = 0
    for pref in ("include", "exclude"):
        for enf in ("enforced", "unenforced"):
            epg = out.external_epg(
                f"niwaki-it-attr-{n}",
                preferred_group_member=pref,
                policy_control_enforcement=enf,
                qos_class=QOS[n % len(QOS)],
                description=f"EPG pref {pref}, enforce {enf}.",
            )
            epg.subnet(f"10.20{n}.0.0/16", scope="import-security")
            n += 1

    # The full contract-label set + provide/consume/intra-EPG verbs on one EPG.
    labelled = out.external_epg("niwaki-it-labelled", description="EPG with every label + verbs.")
    labelled.subnet("10.210.0.0/16", scope="import-security")
    labelled.provide("niwaki-it-epg-ctr")
    labelled.consume("niwaki-it-epg-ctr")
    labelled.intra_epg("niwaki-it-epg-ctr")
    labelled.consumer_contract_label("cons-ctr", tag="cyan")
    labelled.provider_contract_label("prov-ctr", tag="magenta")
    labelled.consumer_subject_label("cons-subj", complement=False, tag="blue")
    labelled.provider_subject_label("prov-subj", complement=True, tag="red")

    t.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — operator only; removes this file's L3Out and VRF."""
    for dn in (
        f"uni/tn-{TN}/out-{L3OUT}",
        f"uni/tn-{TN}/ctx-{VRF}",
        f"uni/tn-{TN}/prof-niwaki-it-epg-rc",
        f"uni/tn-{TN}/brc-niwaki-it-epg-ctr",
    ):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
