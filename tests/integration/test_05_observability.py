"""Act 5 — observability: SPAN, NetFlow and QoS requirements.

The same makers occupy three positions (tenant, access, fabric), and a tenant
SPAN session reaches a filter group that only exists under `infra`: one
design, two domains, one closed-world resolution.

Two mechanisms earn their keep here:

* ``ref()`` — an ERSPAN destination is not a bare edge: the relation carries
  the collector's IP, the source prefix, the TTL and the MTU;
* curated **verbs** — a QoS requirement points at the same policing class
  twice (ingress and egress), which automatic resolution cannot tell apart.

Like every act, this one wipes what it owns at its START and leaves the state
on the simulator afterwards.
"""

from __future__ import annotations

import pytest

from niwaki import Niwaki
from niwaki.design import Cursor, design, ref
from tests.integration.conftest import (
    NETFLOW_EXPORTER,
    NETFLOW_MONITOR,
    NETFLOW_RECORD,
    SPAN_FILTER_GRP,
    TENANT,
    wipe_observability,
)

pytestmark = pytest.mark.integration

APP = "obs-shop"
EPG = "obs-web"
SPAN_SRC_GRP = "niwaki-span-src"
SPAN_DST_GRP = "niwaki-span-dst"


def observability_design() -> Cursor:
    """SPAN, NetFlow and QoS — across infra and the walkthrough tenant."""
    cfg = design()

    infra = cfg.infra()
    # The only home of SPAN filter groups is `uni/infra`.
    filters = infra.filter_group(SPAN_FILTER_GRP)
    # "unspecified", not "0": the ports are naming props, and the APIC rewrites
    # a zero to "unspecified" — the RN, hence the DN, would not match back.
    any_port = ("unspecified",) * 4
    filters.filter_entry("tcp", "10.30.1.0/24", "10.30.2.0/24", *any_port)
    filters.filter_entry("udp", "10.30.1.0/24", "0.0.0.0", *any_port)

    infra.netflow_record(NETFLOW_RECORD, collect_params="count-bytes,count-pkts")
    infra.netflow_exporter(
        NETFLOW_EXPORTER,
        remote_entity_ip="10.30.9.9",
        remote_entity_l4_port="2055",
        # A prefix, not a host: the APIC refuses a source mask longer than /20.
        source_ip_address="10.30.0.0/16",
        exporter_netflow_version_format="v9",
    )
    infra.netflow_monitor(NETFLOW_MONITOR).bind(
        netflow_record=NETFLOW_RECORD, netflow_exporter=NETFLOW_EXPORTER
    )

    tenant = cfg.tenant(TENANT)
    tenant.vrf("prod")
    tenant.bd("obs-bd", unicast_routing=True).bind(vrf="prod").subnet("10.30.7.1/24")
    tenant.dpp_policy("obs-dpp-in", rate=100, rate_unit="mega")
    tenant.dpp_policy("obs-dpp-out", rate=50, rate_unit="mega")

    # One requirement, two relations to the same class — the verbs tell them apart.
    qos = tenant.qos_requirement("obs-qos")
    qos.dscp_marking(mark="AF31")
    qos.ingress_dpp("obs-dpp-in").egress_dpp("obs-dpp-out")

    epg = tenant.app(APP).epg(EPG)
    epg.bind(bd="obs-bd", qos_requirement="obs-qos")

    # A tenant SPAN session pointing at an infra filter group.
    src = tenant.span_source_group(SPAN_SRC_GRP, administrative_state="enabled")
    src.bind(filter_group=SPAN_FILTER_GRP)
    src.span_label(SPAN_DST_GRP)  # a source group matches its destination by name
    src.span_source("src-both", direction_ingress_egress_both="both").bind(epg=EPG)
    src.span_source("src-in", direction_ingress_egress_both="in").bind(epg=EPG)

    # ERSPAN: the destination relation carries the collector's parameters.
    dst = tenant.span_destination_group(SPAN_DST_GRP)
    dst.span_destination("erspan").bind(
        epg=ref(EPG, ip="10.30.8.8", src_ip_prefix="10.30.1.0/24", ttl=64, mtu=1518, ver="ver2")
    )
    return cfg


class Test5Observability:
    """Tenant + access: SPAN sessions, NetFlow, and per-EPG QoS."""

    def test_01_the_observability_design_pushes_atomically(self, live_aci: Niwaki) -> None:
        wipe_observability(live_aci)
        cfg = observability_design()
        assert cfg.push(live_aci, mode="plan").has_changes

        report = cfg.push(live_aci)
        assert report.request_count == 1

    def test_02_every_property_round_trips(self, live_aci: Niwaki) -> None:
        assert observability_design().push(live_aci, mode="plan").has_changes is False

    def test_03_span_crosses_domains(self, live_aci: Niwaki) -> None:
        """The tenant's source group points at a filter group under infra."""
        src = live_aci.tenant(TENANT).span_source_group(SPAN_SRC_GRP)
        assert src.read().administrative_state == "enabled"

        to_filters = src.query("spanRsSrcGrpToFilterGrp").first()
        assert to_filters is not None
        assert to_filters.target_dn == f"uni/infra/filtergrp-{SPAN_FILTER_GRP}"

        sources = {s.name: s for s in src.query("spanSrc").fetch()}
        assert {s.direction_ingress_egress_both for s in sources.values()} == {"both", "in"}

    def test_04_erspan_destination_carries_its_parameters(self, live_aci: Niwaki) -> None:
        """``ref()`` puts the collector's IP on the relation, not on an MO."""
        dst = live_aci.tenant(TENANT).span_destination_group(SPAN_DST_GRP)
        relation = dst.query("spanRsDestEpg").first()
        assert relation is not None
        assert relation.ip == "10.30.8.8"
        assert relation.src_ip_prefix == "10.30.1.0/24"
        assert relation.ttl == 64  # a TTL is a number

    def test_05_netflow_monitor_reads_back(self, live_aci: Niwaki) -> None:
        infra = live_aci.root.infra()
        exporter = infra.netflow_exporter(NETFLOW_EXPORTER).read()
        assert exporter.remote_entity_ip == "10.30.9.9"
        assert exporter.exporter_netflow_version_format == "v9"

        monitor = infra.netflow_monitor(NETFLOW_MONITOR)
        assert monitor.query("netflowRsMonitorToExporter").count() == 1
        assert monitor.query("netflowRsMonitorToRecord").count() == 1

    def test_06_ingress_and_egress_policing_are_told_apart(self, live_aci: Niwaki) -> None:
        qos = live_aci.tenant(TENANT).qos_requirement("obs-qos")
        ingress = qos.query("qosRsIngressDppPol").first()
        egress = qos.query("qosRsEgressDppPol").first()
        assert ingress is not None and egress is not None
        assert ingress.name == "obs-dpp-in"
        assert egress.name == "obs-dpp-out"
        assert qos.query("qosEpDscpMarking").first().mark == "AF31"  # type: ignore[union-attr]

        # And the EPG reaches the requirement — the bind the EPG wave left open.
        epg = live_aci.tenant(TENANT).app(APP).epg(EPG)
        assert epg.query("fvRsQosRequirement").count() == 1
