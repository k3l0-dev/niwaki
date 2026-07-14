"""Golden wire format — L2Out, management EPGs, endpoint tags, address pools.

Offline counterpart of the edge act.  What it pins:

* the **L2Out** (bridged outside) end to end — node profile, interface
  profile, literal-DN path, external EPG with its labels and contract verbs;
* the **management EPGs** — in-band and out-of-band — and the out-of-band
  contract they carry: the out-of-band world speaks ``provide``/``consume``
  like every other EPG, over relation classes of its own;
* **endpoint tags**, which is what an ESG ``tag_selector`` matches;
* the last closed-world holes: an L2 domain under ``uni``, a fallback route
  group under the VRF, an IP address pool under the tenant.
"""

from __future__ import annotations

from niwaki.design import Cursor, design
from niwaki.design._compiler import compile_ops
from niwaki.design._resolver import resolve


def edge_and_management() -> Cursor:
    """One design: the L2 edge, the management EPGs, the tags."""
    cfg = design()

    infra = cfg.infra()
    infra.vlan_pool("vp", "static").range("vlan-220", "vlan-229")
    cfg.l2_dom("l2dom").bind(vlan_pool="vp")

    tenant = cfg.tenant("T")
    tenant.vrf("v").fallback_route_group("fbr").fallback_route("0.0.0.0/0")
    tenant.bd("b").bind(vrf="v")
    tenant.filter("f").entry("e", tcp=8080)
    tenant.contract("c").subject("s").bind(filter="f")
    tenant.oob_contract("oob-c").subject("oob-s").bind(filter="f")

    l2out = tenant.l2out("l2")
    l2out.bind(bd="b", domain="l2dom")
    l2out.node_profile("np").interface_profile("ifp").static_path(
        "topology/pod-1/paths-101/pathep-[eth1/20]"
    )
    external = l2out.external_epg("l2-epg")
    external.provide("c").consume("c")
    external.consumer_label("gold", tag="green")

    # Management: the in-band EPG takes ordinary contracts, the out-of-band
    # one takes the out-of-band contract — same verb, its own Rs class.
    mgmt = tenant.management_profile("default")
    mgmt.in_band_epg("inb", encap="vlan-300").bind(bd="b").provide("c")
    mgmt.out_of_band_epg("oob").provide("oob-c")
    tenant.external_management_entity("default").external_management_epg("ext").consume("oob-c")

    tags = tenant.endpoint_tags()
    tags.mac_endpoint("00:11:22:33:44:99", "b", name="tagged-mac")
    tags.ip_endpoint("10.0.1.99", "v", name="tagged-ip")

    tenant.ip_address_pool("pool").ip_address_block("10.5.0.1", "10.5.0.50")
    return cfg


def _flatten(cursor: Cursor) -> dict[str, dict[str, str]]:
    """The DN → attributes map the push engine writes, minus the ``dn`` echo."""
    root = cursor.design_node.root()
    flat = {}
    for op in compile_ops(root, resolve(root)):
        assert op.payload is not None
        ((_, body),) = op.payload.items()
        flat[op.dn] = {k: v for k, v in body["attributes"].items() if k != "dn"}
    return flat


class TestL2OutGolden:
    def test_l2out_reaches_bd_domain_path_and_contracts(self) -> None:
        flat = _flatten(edge_and_management())
        l2 = "uni/tn-T/l2out-l2"
        assert {dn: a for dn, a in flat.items() if dn.startswith(l2)} == {
            l2: {"name": "l2"},
            f"{l2}/rseBd": {"tnFvBDName": "b"},
            f"{l2}/rsl2DomAtt": {"tDn": "uni/l2dom-l2dom"},
            f"{l2}/lnodep-np": {"name": "np"},
            f"{l2}/lnodep-np/lifp-ifp": {"name": "ifp"},
            f"{l2}/lnodep-np/lifp-ifp/rspathL2OutAtt-[topology/pod-1/paths-101/pathep-[eth1/20]]": {
                "tDn": "topology/pod-1/paths-101/pathep-[eth1/20]"
            },
            f"{l2}/instP-l2-epg": {"name": "l2-epg"},
            f"{l2}/instP-l2-epg/rsprov-c": {"tnVzBrCPName": "c"},
            f"{l2}/instP-l2-epg/rscons-c": {"tnVzBrCPName": "c"},
            f"{l2}/instP-l2-epg/conslbl-gold": {"name": "gold", "tag": "green"},
        }

    def test_the_l2_domain_is_declarable(self) -> None:
        """The last non-fabric closed-world hole: ``l2extDomP`` under ``uni``."""
        flat = _flatten(edge_and_management())
        assert flat["uni/l2dom-l2dom"] == {"name": "l2dom"}
        assert flat["uni/l2dom-l2dom/rsvlanNs"] == {"tDn": "uni/infra/vlanns-[vp]-static"}


class TestManagementGolden:
    def test_in_band_epg_takes_ordinary_contracts(self) -> None:
        flat = _flatten(edge_and_management())
        inb = "uni/tn-T/mgmtp-default/inb-inb"
        assert flat[inb] == {"name": "inb", "encap": "vlan-300"}
        assert flat[f"{inb}/rsmgmtBD"] == {"tnFvBDName": "b"}
        assert flat[f"{inb}/rsprov-c"] == {"tnVzBrCPName": "c"}

    def test_out_of_band_speaks_the_same_words_over_its_own_classes(self) -> None:
        """``provide``/``consume`` again — but ``mgmtRsOoBProv``/``mgmtRsOoBCons``."""
        flat = _flatten(edge_and_management())
        assert flat["uni/tn-T/mgmtp-default/oob-oob/rsooBProv-oob-c"] == {
            "tnVzOOBBrCPName": "oob-c"
        }
        assert flat["uni/tn-T/extmgmt-default/instp-ext/rsooBCons-oob-c"] == {
            "tnVzOOBBrCPName": "oob-c"
        }


class TestTagsAndPoolsGolden:
    def test_endpoint_tags_are_what_an_esg_tag_selector_matches(self) -> None:
        flat = _flatten(edge_and_management())
        assert flat["uni/tn-T/eptags"] == {}  # a singleton container
        assert flat["uni/tn-T/eptags/epmactag-00:11:22:33:44:99-[b]"] == {
            "mac": "00:11:22:33:44:99",
            "bdName": "b",
            "name": "tagged-mac",
        }
        assert flat["uni/tn-T/eptags/epiptag-[10.0.1.99]-v"] == {
            "ip": "10.0.1.99",
            "ctxName": "v",
            "name": "tagged-ip",
        }

    def test_fallback_route_group_and_address_pool(self) -> None:
        flat = _flatten(edge_and_management())
        assert flat["uni/tn-T/ctx-v/fbrg-fbr/pfx-[0.0.0.0/0]"] == {"fbrPrefix": "0.0.0.0/0"}
        assert flat["uni/tn-T/addrinst-pool/fromaddr-[10.5.0.1]-toaddr-[10.5.0.50]"] == {
            "from": "10.5.0.1",
            "to": "10.5.0.50",
        }
