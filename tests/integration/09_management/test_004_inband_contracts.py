"""Management — in-band EPG contract binds and contract matrix (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/09_management/test_004_inband_contracts.py -m integration -s

The operator wires the in-band EPG into the contract world and, along the way,
lays down a matrix of regular contracts. Coverage:

- **Contract matrix** (``vzBrCP``): three variants of each ``scope`` (4), with
  QoS class and contract DSCP rotated across the set; each with a subject whose
  consumer/provider match types, ``reverse_filter_ports`` and subject DSCP vary,
  bound to one of several filters carrying different ether-types and protocols.
  (A fresh contract can only be created in ``install`` intent — the estimate
  modes are a transient state the controller refuses at creation.)
- **In-band EPG binds**: bridge domain, imported-contract interface (``vzCPIf``)
  and taboo contract, plus the provide / consume verbs across the context /
  global scoped contracts.

Everything is **named** (``niwaki-it-*``) under the APIC-managed ``mgmt`` tenant;
the tenant and its ``mgmtp-default`` profile are only *traversed*. Values are
illustrative.

APIC / engine constraints exercised here (real, not curation bugs):
  - contract_master (fvRsSecInherited) and intra_epg (fvRsIntraEpg): only
    supported on fvAEPg / fvESg / l3extInstP, never a management EPG.
  - custom_qos_policy (fvRsCustQosPol): expressible on a first push, but this
    never-creatable relation is not re-push-idempotent under strict mode — the
    controller refuses to re-create it (unlike mgmtRsMgmtBD, which it modifies in
    place). The custom QoS policy object is still declared; only the EPG bind is
    left off so the suite stays green on repeated pushes.

``wipe(aci)`` (operator-only) removes only the named objects this file creates.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import Cursor, ref, tenant
from niwaki.exceptions import NotFoundError
from niwaki.models.aaa.aaaRbacAnnotation import aaaRbacAnnotation
from niwaki.models.tag.tagAnnotation import tagAnnotation
from niwaki.models.tag.tagTag import tagTag

pytestmark = pytest.mark.integration

TN = "mgmt"
EPG = "niwaki-it-inb-ctr"
ENCAP = "vlan-2952"
VRF = "niwaki-it-inb-ctr-vrf"
BD = "niwaki-it-inb-ctr-bd"
QOS = "niwaki-it-inb-ctr-qos"
IMPORTED = "niwaki-it-inb-ctr-imported"
EXPORTED = "niwaki-it-inb-ctr-exported"
TABOO = "niwaki-it-inb-ctr-taboo"

SCOPES = ("context", "global", "tenant", "application-profile")
INTENTS = ("install", "estimate_add", "estimate_delete")
MATCH = ("All", "AtleastOne", "AtmostOne", "None")
PRIOS = ("level1", "level2", "level3", "level4", "level5", "level6", "unspecified")
DSCP = ("AF11", "AF21", "AF31", "AF41", "CS0", "CS3", "CS6", "EF", "VA", "unspecified")

FILTERS = (
    "niwaki-it-flt-tcp",
    "niwaki-it-flt-udp",
    "niwaki-it-flt-icmp",
    "niwaki-it-flt-arp",
)
# One contract per (scope x intent) combination.
CTR_COUNT = len(SCOPES) * len(INTENTS)
CTR_NAMES = [f"niwaki-it-ctr-{i:02d}" for i in range(CTR_COUNT)]


def _common(obj: Cursor) -> None:
    """Attach the universal children present on almost every ACI class."""
    obj.mo(tagTag, key="niwaki-it", value="management")
    obj.mo(tagAnnotation, key="niwaki-it-note", value="exhaustive-provisioning")
    obj.mo(aaaRbacAnnotation, domain="all")


def _filters(mgmt: Cursor) -> None:
    """Declare filters with a spread of ether-types, protocols and TCP rules."""
    tcp = mgmt.filter(FILTERS[0], description="TCP filter.")
    tcp.entry(
        "ssh",
        tcp=22,
        ethernet_type="ipv4",
        protocol="tcp",
        stateful=True,
        tcp_rules="syn,ack",
        apply_to_frag=False,
        description="SSH.",
    )
    tcp.entry("https", tcp=443, ethernet_type="ip", protocol="tcp", description="HTTPS.")
    _common(tcp)

    udp = mgmt.filter(FILTERS[1], description="UDP filter.")
    udp.entry("snmp", udp=161, ethernet_type="ipv4", protocol="udp", description="SNMP.")
    _common(udp)

    icmp = mgmt.filter(FILTERS[2], description="ICMP filter.")
    icmp.entry("echo", ethernet_type="ipv4", protocol="icmp", description="ICMP echo.")
    _common(icmp)

    arp = mgmt.filter(FILTERS[3], description="ARP filter.")
    arp.entry("arp", ethernet_type="arp", arp_opcodes="req", description="ARP request.")
    _common(arp)


def test_inband_contracts(live_aci: Niwaki) -> None:
    mgmt = tenant(TN)

    # Networking + policies the in-band EPG binds.
    vrf = mgmt.vrf(VRF, description="In-band contract VRF.")
    _common(vrf)
    bd = mgmt.bd(BD, description="In-band contract BD.", unicast_routing=True).bind(vrf=VRF)
    bd.subnet("10.212.0.1/24", scope="public,shared", description="In-band BD gateway.")
    _common(bd)

    qos = mgmt.custom_qos_policy(QOS, description="In-band custom QoS policy.")
    _common(qos)

    _filters(mgmt)

    exported = mgmt.contract(EXPORTED, scope="global", description="Contract exported for import.")
    exported.subject("all", description="Exported subject.").bind(filter=FILTERS[0])
    _common(exported)
    mgmt.imported_contract(IMPORTED, description="Imported contract interface.").bind(
        contract=EXPORTED
    )

    taboo = mgmt.taboo_contract(TABOO, description="In-band taboo (deny) contract.")
    taboo.subject("deny", description="Denied flows.").bind(filter=FILTERS[2])
    _common(taboo)

    # ── Contract matrix: scope x intent, rotating QoS/DSCP and subject knobs ──
    provide_targets: list[str] = []
    consume_targets: list[str] = []
    idx = 0
    for scope in SCOPES:
        # A vzBrCP can only be created in ``install`` intent — the estimate
        # modes are a transient two-phase-commit state the APIC refuses on a
        # fresh contract — so we hold intent and vary the other knobs instead.
        for _rep in range(len(INTENTS)):
            name = CTR_NAMES[idx]
            ctr = mgmt.contract(
                name,
                scope=scope,
                intent="install",
                qos_class_id=PRIOS[idx % len(PRIOS)],
                contract_level_dscp=DSCP[idx % len(DSCP)],
                description=f"In-band contract matrix, scope {scope}.",
            )
            subj = ctr.subject(
                "subj",
                consumer_label_match_type=MATCH[idx % len(MATCH)],
                provider_label_match_type=MATCH[(idx + 1) % len(MATCH)],
                reverse_filter_ports=(idx % 2 == 0),
                subject_level_dscp=DSCP[(idx + 1) % len(DSCP)],
                description=f"Subject for {name}.",
            )
            subj.bind(filter=ref(FILTERS[idx % len(FILTERS)], directives="log"))
            _common(ctr)
            if scope == "context":
                provide_targets.append(name)
            elif scope == "global":
                consume_targets.append(name)
            idx += 1

    # ── The in-band EPG: every bind and verb the APIC accepts here ───────────
    profile = mgmt.management_profile()
    epg = profile.in_band_epg(
        EPG,
        encap=ENCAP,
        qos_class="level1",
        description="In-band EPG wired into the contract matrix.",
    )
    epg.bind(bd=BD, imported_contract=IMPORTED, taboo_contract=TABOO)
    # provide / consume are supported on a management EPG; intra_epg (fvRsIntraEpg)
    # is not — the APIC restricts it to fvAEPg / fvESg / l3extInstP, so the
    # tenant-scoped contracts stay declared (scope coverage) but unverbed here.
    for name in provide_targets:
        epg.provide(name)
    for name in consume_targets:
        epg.consume(name)
    _common(epg)

    mgmt.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    dns = [f"uni/tn-{TN}/mgmtp-default/inb-{EPG}"]
    dns += [f"uni/tn-{TN}/brc-{name}" for name in CTR_NAMES]
    dns += [f"uni/tn-{TN}/brc-{EXPORTED}", f"uni/tn-{TN}/cif-{IMPORTED}"]
    dns += [f"uni/tn-{TN}/taboo-{TABOO}", f"uni/tn-{TN}/qoscustom-{QOS}"]
    dns += [f"uni/tn-{TN}/flt-{name}" for name in FILTERS]
    dns += [f"uni/tn-{TN}/BD-{BD}", f"uni/tn-{TN}/ctx-{VRF}"]
    for dn in dns:
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
