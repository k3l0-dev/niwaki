"""Golden wire format — the EPG/ESG world under an application profile.

Offline counterpart of the live EPG-world act: every curated position under
``fvAEPg``/``fvESg`` (subnets, static endpoints, uSeg criteria, virtual IPs,
Fibre-Channel paths, ESG selectors) plus the contract objects an EPG points
at (taboo, imported contract, monitoring policy).

The assertion is the flattened ``{DN: attributes}`` view the push engine
sends: it pins both the RN formats (``stcep-<mac>-type-<type>``,
``tagselectorkey-[k]-value-[v]``, ``subnet-[10.0.1.1/24]`` …) and the wire
property names.  Any drift here is a wire regression.

Every relation is a ``bind()`` or a curated verb — no ``.mo()`` escape; the
Fibre-Channel path is a literal-DN maker, like ``static_path``.
"""

from __future__ import annotations

from niwaki.design import Cursor, tenant
from niwaki.design._compiler import compile_ops
from niwaki.design._resolver import resolve


def epg_world() -> Cursor:
    """One tenant exercising every curated position of the EPG/ESG world."""
    return (
        tenant("T")
        .vrf("v")
        .bd("b").bind(vrf="v")
        .filter("f").entry("e", tcp=8080)
        .contract("web").subject("s").bind(filter="f")
        .taboo_contract("tb").subject("ts").bind(filter="f")
        .imported_contract("imp").bind(contract="web")
        .monitoring_policy("mon")
        .custom_qos_policy("qos")
        .app("ap")
            .epg("web")
                .bind(bd="b", contract_master="db", imported_contract="imp",
                      taboo_contract="tb", monitoring_policy="mon", custom_qos_policy="qos")
                .provide("web").consume("web").intra_epg("web")
                .subnet("10.0.1.1/24", scope="public", preferred=True)
                .static_endpoint("00:11:22:33:44:55", "silent-host",
                                 encap="vlan-101", ip_address="10.0.1.9")
                    .static_ip("10.0.1.10")
                .virtual_ip("10.0.1.100")
                .fc_path("topology/pod-1/paths-101/pathep-[eth1/10]",
                         vsan="vsan-100", vsan_mode="native")
            .epg("db")
            .epg("useg", attribute_based_epg=True).bind(bd="b")
                .criterion(matching_rule_type="any")
                    .ip_attribute("ip1", ip_address="10.0.1.50")
                    .mac_attribute("m1", macaddress="00:AA:BB:CC:DD:EE")
                    .vm_attribute("vm1", attribute_type="guest-os", operator="contains",
                                  value="Ubuntu")
                    .dns_attribute("dns1", domain_name_filter="*.corp.local")
                    .sub_criterion("sub1", matching_rule_type="all")
                        .vm_attribute("vm2", attribute_type="vm-name", operator="equals",
                                      value="web-01")
            .esg("secure", policy_control_enforcement="enforced", qos_class="level3")
                .bind(vrf="v", contract_master="secure2")
                .provide("web").consume("web").intra_epg("web")
                .ep_selector("ip=='10.0.1.7'")
                .epg_selector("uni/tn-T/ap-ap/epg-db")
                .tag_selector("env", "prod", match_value_operator="equals")
            .esg("secure2").bind(vrf="v")
    )  # fmt: skip


def _flatten(cursor: Cursor) -> dict[str, dict[str, str]]:
    """The DN → attributes map the push engine writes, minus the ``dn`` echo."""
    root = cursor.design_node.root()
    flat = {}
    for op in compile_ops(root, resolve(root)):
        assert op.payload is not None
        ((_, body),) = op.payload.items()
        flat[op.dn] = {k: v for k, v in body["attributes"].items() if k != "dn"}
    return flat


class TestEpgWorldGolden:
    """The EPG, its uSeg criterion, its ESG siblings — down to the wire."""

    def test_epg_children(self) -> None:
        flat = _flatten(epg_world())
        epg = "uni/tn-T/ap-ap/epg-web"
        assert {dn: a for dn, a in flat.items() if dn.startswith(epg)} == {
            epg: {"name": "web"},
            # Subnets, static endpoints, virtual IPs, Fibre-Channel paths.
            f"{epg}/subnet-[10.0.1.1/24]": {
                "ip": "10.0.1.1/24",
                "scope": "public",
                "preferred": "true",
            },
            f"{epg}/stcep-00:11:22:33:44:55-type-silent-host": {
                "mac": "00:11:22:33:44:55",
                "type": "silent-host",
                "encap": "vlan-101",
                "ip": "10.0.1.9",
            },
            f"{epg}/stcep-00:11:22:33:44:55-type-silent-host/ip-[10.0.1.10]": {"addr": "10.0.1.10"},
            f"{epg}/vip-[10.0.1.100]": {"addr": "10.0.1.100"},
            f"{epg}/rsfcPathAtt-[topology/pod-1/paths-101/pathep-[eth1/10]]": {
                "tDn": "topology/pod-1/paths-101/pathep-[eth1/10]",
                "vsan": "vsan-100",
                "vsanMode": "native",
            },
            # Binds — name flavor (bd, contracts, policies) and dn flavor
            # (contract master, through the abstract fvEPg target).
            f"{epg}/rsbd": {"tnFvBDName": "b"},
            f"{epg}/rssecInherited-[uni/tn-T/ap-ap/epg-db]": {"tDn": "uni/tn-T/ap-ap/epg-db"},
            f"{epg}/rsconsIf-imp": {"tnVzCPIfName": "imp"},
            f"{epg}/rsprotBy-tb": {"tnVzTabooName": "tb"},
            f"{epg}/rscustQosPol": {"tnQosCustomPolName": "qos"},
            f"{epg}/rsAEPgMonPol": {"tnMonEPGPolName": "mon"},
            # Verbs.
            f"{epg}/rsprov-web": {"tnVzBrCPName": "web"},
            f"{epg}/rscons-web": {"tnVzBrCPName": "web"},
            f"{epg}/rsintraEpg-web": {"tnVzBrCPName": "web"},
        }

    def test_useg_criterion(self) -> None:
        """The criterion is a singleton (``crtrn``); sub-criteria nest under it."""
        flat = _flatten(epg_world())
        crtrn = "uni/tn-T/ap-ap/epg-useg/crtrn"
        assert flat["uni/tn-T/ap-ap/epg-useg"] == {"name": "useg", "isAttrBasedEPg": "true"}
        assert {dn: a for dn, a in flat.items() if dn.startswith(crtrn)} == {
            crtrn: {"match": "any"},
            f"{crtrn}/ipattr-ip1": {"name": "ip1", "ip": "10.0.1.50"},
            f"{crtrn}/macattr-m1": {"name": "m1", "mac": "00:AA:BB:CC:DD:EE"},
            f"{crtrn}/vmattr-vm1": {
                "name": "vm1",
                "type": "guest-os",
                "operator": "contains",
                "value": "Ubuntu",
            },
            f"{crtrn}/dnsattr-dns1": {"name": "dns1", "filter": "*.corp.local"},
            f"{crtrn}/crtrn-sub1": {"name": "sub1", "match": "all"},
            f"{crtrn}/crtrn-sub1/vmattr-vm2": {
                "name": "vm2",
                "type": "vm-name",
                "operator": "equals",
                "value": "web-01",
            },
        }

    def test_esg_selectors_and_scope(self) -> None:
        flat = _flatten(epg_world())
        esg = "uni/tn-T/ap-ap/esg-secure"
        assert {dn: a for dn, a in flat.items() if dn.startswith(f"{esg}/")} == {
            # An ESG lives in a VRF (fvRsScope) — not in a BD.
            f"{esg}/rsscope": {"tnFvCtxName": "v"},
            f"{esg}/epselector-[ip=='10.0.1.7']": {"matchExpression": "ip=='10.0.1.7'"},
            f"{esg}/epgselector-[uni/tn-T/ap-ap/epg-db]": {"matchEpgDn": "uni/tn-T/ap-ap/epg-db"},
            f"{esg}/tagselectorkey-[env]-value-[prod]": {
                "matchKey": "env",
                "matchValue": "prod",
                "valueOperator": "equals",
            },
            f"{esg}/rssecInherited-[uni/tn-T/ap-ap/esg-secure2]": {
                "tDn": "uni/tn-T/ap-ap/esg-secure2"
            },
            f"{esg}/rsprov-web": {"tnVzBrCPName": "web"},
            f"{esg}/rscons-web": {"tnVzBrCPName": "web"},
            f"{esg}/rsintraEpg-web": {"tnVzBrCPName": "web"},
        }
        assert flat[esg] == {"name": "secure", "pcEnfPref": "enforced", "prio": "level3"}

    def test_contract_objects_the_epg_points_at(self) -> None:
        """Taboo (with its subject) and imported contract (pointing at a contract)."""
        flat = _flatten(epg_world())
        assert flat["uni/tn-T/taboo-tb"] == {"name": "tb"}
        assert flat["uni/tn-T/taboo-tb/tsubj-ts"] == {"name": "ts"}
        assert flat["uni/tn-T/taboo-tb/tsubj-ts/rsdenyRule-f"] == {"tnVzFilterName": "f"}
        assert flat["uni/tn-T/cif-imp"] == {"name": "imp"}
        assert flat["uni/tn-T/cif-imp/rsif"] == {"tDn": "uni/tn-T/brc-web"}
        assert flat["uni/tn-T/monepg-mon"] == {"name": "mon"}

    def test_contract_master_resolves_epg_and_esg_alike(self) -> None:
        """One alias, one abstract target (``fvEPg``) — two concrete classes."""
        flat = _flatten(epg_world())
        masters = {dn for dn in flat if "secInherited" in dn}
        assert masters == {
            "uni/tn-T/ap-ap/epg-web/rssecInherited-[uni/tn-T/ap-ap/epg-db]",
            "uni/tn-T/ap-ap/esg-secure/rssecInherited-[uni/tn-T/ap-ap/esg-secure2]",
        }
