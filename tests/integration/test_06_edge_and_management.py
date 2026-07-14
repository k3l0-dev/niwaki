"""Act 6 — the L2 edge and the management EPGs.

The bridged outside (`l2extOut`) end to end, the endpoint tags an ESG tag
selector matches, and the management EPGs — which is where the out-of-band
contract of act 3 finally finds someone to provide and consume it.

``ref()`` earns its keep again: an L2Out reaches its bridge domain through a
relation that carries the **encap**, and a management EPG reaches a node
through a relation that carries the node's **management address**.

Safety note: this act never touches the APIC's *default* out-of-band EPG.  It
declares EPGs of its own and binds no node to them, so out-of-band access to
the simulator cannot be restricted by anything pushed here.

Like every act, it wipes what it owns at its START and leaves the state on the
simulator afterwards.
"""

from __future__ import annotations

import pytest

from niwaki import Niwaki
from niwaki.design import Cursor, design, ref
from tests.integration.conftest import (
    EXT_MGMT,
    L2_DOM,
    MGMT_TENANT,
    OOB_EPG,
    TENANT,
    VLAN_POOL,
    wipe_edge,
    wipe_management,
)

pytestmark = pytest.mark.integration

L2OUT = "niwaki-l2out"
L2_PATH_DN = "topology/pod-1/paths-101/pathep-[eth1/21]"
OOB_CONTRACT = "niwaki-oob"  # declared by act 3's contract world


def edge_design() -> Cursor:
    """The L2 edge: an L2Out on act-2's cabling, plus tags and pools."""
    cfg = design()

    # An L2 domain fed by act-2's VLAN pool — the last closed-world hole.
    cfg.infra().vlan_pool(VLAN_POOL, "static")  # upsert: act 2 owns it
    cfg.l2_dom(L2_DOM).bind(vlan_pool=VLAN_POOL)

    tenant = cfg.tenant(TENANT)
    vrf = tenant.vrf("prod")
    tenant.bd("l2-bd", unicast_routing=False, arp_flooding=True).bind(vrf="prod")
    tenant.filter("l2-any").entry("all", ethernet_type="unspecified")
    tenant.contract("l2-web").subject("l2-subj").bind(filter="l2-any")

    l2out = tenant.l2out(L2OUT)
    # The BD relation carries the encap — a plain edge could not say it.
    l2out.bind(bd=ref("l2-bd", encap="vlan-230"), domain=L2_DOM)
    l2out.node_profile("np-101").interface_profile("ifp-101").static_path(L2_PATH_DN)
    external = l2out.external_epg("l2-ext", preferred_group_member="exclude")
    external.provide("l2-web").consume("l2-web")
    external.consumer_label("l2-gold", tag="green")

    # A fallback route group on the VRF, and an IP address pool.
    fallback = vrf.fallback_route_group("niwaki-fbr")
    fallback.fallback_route("0.0.0.0/0")
    fallback.fallback_member("10.30.1.254")
    tenant.ip_address_pool("niwaki-pool").ip_address_block("10.30.20.1", "10.30.20.50")

    # Endpoint tags — what an ESG tag_selector matches.
    tags = tenant.endpoint_tags()
    tags.mac_endpoint("00:11:22:33:44:F1", "l2-bd", name="tagged-mac")
    tags.ip_endpoint("10.30.7.99", "prod", name="tagged-ip")
    return cfg


def management_design() -> Cursor:
    """The management EPGs — in the management tenant, where they belong.

    The out-of-band contract comes from act 3; here it is provided by an
    out-of-band EPG and consumed by an external management network profile.
    Neither is the APIC's ``default``, and no node is bound to them.
    """
    cfg = design().tenant(MGMT_TENANT)  # upsert: mgmt ships with the APIC
    cfg.oob_contract(OOB_CONTRACT)  # upsert: act 3 declared it

    profile = cfg.management_profile("default")
    profile.out_of_band_epg(OOB_EPG, qos_class="level3").provide(OOB_CONTRACT)

    external = cfg.external_management_entity("default").external_management_epg(EXT_MGMT)
    external.consume(OOB_CONTRACT)
    external.external_subnet("10.0.0.0/8")
    external.external_subnet("172.16.0.0/12")
    return cfg


class Test6L2Edge:
    """Tenants > Networking > L2Outs, plus endpoint tags and pools."""

    def test_01_the_edge_design_pushes_atomically(self, live_aci: Niwaki) -> None:
        wipe_edge(live_aci)
        cfg = edge_design()
        assert cfg.push(live_aci, mode="plan").has_changes

        report = cfg.push(live_aci)
        assert report.request_count == 1

    def test_02_every_property_round_trips(self, live_aci: Niwaki) -> None:
        assert edge_design().push(live_aci, mode="plan").has_changes is False

    def test_03_the_l2out_reaches_bd_domain_and_path(self, live_aci: Niwaki) -> None:
        l2out = live_aci.tenant(TENANT).l2out(L2OUT)

        # ref() put the encap on the relation itself.
        bd_relation = l2out.query("l2extRsEBd").first()
        assert bd_relation is not None
        assert bd_relation.encap == "vlan-230"

        domain = l2out.query("l2extRsL2DomAtt").first()
        assert domain is not None
        assert domain.target_dn == f"uni/l2dom-{L2_DOM}"

        paths = l2out.query("l2extRsPathL2OutAtt").fetch()
        assert [p.target_dn for p in paths] == [L2_PATH_DN]

    def test_04_the_external_epg_speaks_the_epg_vocabulary(self, live_aci: Niwaki) -> None:
        external = live_aci.tenant(TENANT).l2out(L2OUT).external_epg("l2-ext")
        assert external.read().preferred_group_member == "exclude"
        assert external.query("fvRsProv").count() == 1
        assert external.query("fvRsCons").count() == 1
        assert external.query("vzConsLbl").first().tag == "green"  # type: ignore[union-attr]

    def test_05_tags_pools_and_fallback_routes(self, live_aci: Niwaki) -> None:
        tenant = live_aci.tenant(TENANT)

        tags = tenant.endpoint_tags()
        assert tags.query("fvEpMacTag").first().endpoint_mac_address == "00:11:22:33:44:F1"  # type: ignore[union-attr]
        assert tags.query("fvEpIpTag").first().endpoint_ip_address == "10.30.7.99"  # type: ignore[union-attr]

        blocks = tenant.ip_address_pool("niwaki-pool").query("fvnsUcastAddrBlk").fetch()
        assert [b.ending_ip_address for b in blocks] == ["10.30.20.50"]

        fallback = tenant.vrf("prod").fallback_route_group("niwaki-fbr")
        assert fallback.query("fvFBRoute").count() == 1
        assert fallback.query("fvFBRMember").count() == 1


class Test6Management:
    """Tenant mgmt — the EPGs that carry the out-of-band contract."""

    def test_01_the_management_design_pushes(self, live_aci: Niwaki) -> None:
        wipe_management(live_aci)  # this act owns two EPGs in the mgmt tenant

        cfg = management_design()
        cfg.push(live_aci)
        assert cfg.push(live_aci, mode="plan").has_changes is False

    def test_02_out_of_band_speaks_provide_and_consume(self, live_aci: Niwaki) -> None:
        """Same verbs as any EPG — over ``mgmtRsOoBProv``/``mgmtRsOoBCons``."""
        mgmt = live_aci.tenant(MGMT_TENANT)

        provider = mgmt.management_profile("default").out_of_band_epg(OOB_EPG)
        assert provider.read().qos_class == "level3"
        prov_relation = provider.query("mgmtRsOoBProv").first()
        assert prov_relation is not None
        assert prov_relation.name == OOB_CONTRACT

        consumer = mgmt.external_management_entity("default").external_management_epg(EXT_MGMT)
        cons_relation = consumer.query("mgmtRsOoBCons").first()
        assert cons_relation is not None
        assert cons_relation.name == OOB_CONTRACT

        subnets = {s.subnet for s in consumer.query("mgmtSubnet").fetch()}
        assert subnets == {"10.0.0.0/8", "172.16.0.0/12"}
