"""Golden payloads — fabric, controller and access-policy domains.

Offline counterparts of the live walkthrough acts 1 and 2: the exact APIC
payloads the design DSL compiles for fabric/controller policies and access
policies.  Any change to these dicts is a wire-format regression.

Every relation resolves through ``bind()`` (REFERENCE_MAP, name and dn
flavors, abstract targets) — no ``.mo()`` escapes; the literal-DN
``static_path`` is a curated maker.
"""

from __future__ import annotations

from typing import Any

from niwaki.design import design


def _mo(aci_class: str, attributes: dict[str, str], *children: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"attributes": attributes}
    if children:
        body["children"] = list(children)
    return {aci_class: body}


class TestFabricAndControllerGolden:
    """Act 1 — NTP, DNS, syslog, BGP RR, vPC protection, fabric membership."""

    def test_payload(self) -> None:
        cfg = design()
        fb = cfg.fabric()
        fb.datetime_policy("prod-time", admin_state="enabled").ntp_provider(
            "10.10.10.1", preferred_state=True, min_poll=4, max_poll=6
        )
        dns = fb.dns_profile("default")
        dns.provider("8.8.8.8", prefered_dns_provider=True)
        dns.domain("acme.corp", default=True)
        fb.syslog_group("prod-syslog").remote_destination(
            "10.20.0.5", port=514, severity="warnings"
        )
        bgp = fb.bgp_instance("default")
        bgp.autonomous_system(autonomous_system_number=65001)
        bgp.route_reflector().node(101, pod_id=1)
        fb.vpc_protection().vpc_pair("vpc-101-102", logical_pair_id=101).node(101).node(102)
        cfg.controller().fabric_membership().fabric_node_member(
            "SN101", id=101, name="leaf-01", role="leaf"
        )

        assert cfg.to_payload() == _mo(
            "polUni",
            {},
            _mo(
                "fabricInst",
                {},
                _mo(
                    "datetimePol",
                    {"name": "prod-time", "adminSt": "enabled"},
                    _mo(
                        "datetimeNtpProv",
                        {
                            "name": "10.10.10.1",
                            "preferred": "true",
                            "minPoll": "4",
                            "maxPoll": "6",
                        },
                    ),
                ),
                _mo(
                    "dnsProfile",
                    {"name": "default"},
                    _mo("dnsProv", {"addr": "8.8.8.8", "preferred": "true"}),
                    _mo("dnsDomain", {"name": "acme.corp", "isDefault": "true"}),
                ),
                _mo(
                    "syslogGroup",
                    {"name": "prod-syslog"},
                    _mo(
                        "syslogRemoteDest",
                        {"host": "10.20.0.5", "port": "514", "severity": "warnings"},
                    ),
                ),
                _mo(
                    "bgpInstPol",
                    {"name": "default"},
                    _mo("bgpAsP", {"asn": "65001"}),
                    _mo("bgpRRP", {}, _mo("bgpRRNodePEp", {"id": "101", "podId": "1"})),
                ),
                _mo(
                    "fabricProtPol",
                    {},
                    _mo(
                        "fabricExplicitGEp",
                        {"name": "vpc-101-102", "id": "101"},
                        _mo("fabricNodePEp", {"id": "101"}),
                        _mo("fabricNodePEp", {"id": "102"}),
                    ),
                ),
            ),
            _mo(
                "ctrlrInst",
                {},
                _mo(
                    "fabricNodeIdentPol",
                    {},
                    _mo(
                        "fabricNodeIdentP",
                        {"serial": "SN101", "nodeId": "101", "name": "leaf-01", "role": "leaf"},
                    ),
                ),
            ),
        )


class TestThreeActsOneDesign:
    """The whole walkthrough — fabric, access and tenant — in one design.

    Multi-domain is structural (phases 0-1): one ``design()``, one atomic
    payload, cross-domain binds resolved closed-world.  This pins the shape,
    not every attribute — the per-domain goldens above pin the wire format.
    """

    def test_one_envelope_covers_the_three_acts(self) -> None:
        cfg = design()

        # Act 1 — fabric
        fb = cfg.fabric()
        fb.datetime_policy("niwaki-datetime").ntp_provider("10.10.10.1", preferred_state=True)
        fb.vpc_protection().vpc_pair("vpc-101-102", logical_pair_id=101).node(101).node(102)

        # Act 2 — access policies
        inf = cfg.infra()
        inf.vlan_pool("niwaki-vlans", "static").range("vlan-100", "vlan-199")
        cfg.phys_dom("niwaki-phys").bind(vlan_pool="niwaki-vlans")
        inf.aaep("niwaki-aaep").bind(domain="niwaki-phys")

        # Act 3 — tenant, with a cross-domain bind into act 2
        t = cfg.tenant("niwaki-prod")
        t.vrf("main")
        t.bd("web").bind(vrf="main").subnet("10.0.1.1/24")
        t.app("shop").epg("web").bind(bd="web", domain="niwaki-phys")

        payload = cfg.to_payload()
        domains = [next(iter(child)) for child in payload["polUni"]["children"]]
        assert domains == ["fabricInst", "infraInfra", "physDomP", "fvTenant"]

        # The cross-domain reference resolved against the declared phys-dom.
        tenant_tree = payload["polUni"]["children"][3]["fvTenant"]
        epg = tenant_tree["children"][2]["fvAp"]["children"][0]["fvAEPg"]
        rs_classes = {next(iter(child)): child for child in epg["children"]}
        assert rs_classes["fvRsDomAtt"]["fvRsDomAtt"]["attributes"]["tDn"] == "uni/phys-niwaki-phys"

    def test_staged_ops_cover_all_domains_in_order(self) -> None:
        from niwaki.design._compiler import compile_ops
        from niwaki.design._engine import _toposort
        from niwaki.design._resolver import resolve

        cfg = design()
        cfg.fabric().datetime_policy("t")
        cfg.infra().aaep("a")
        cfg.tenant("p").vrf("v")
        root = cfg.design_node.root()
        waves = _toposort(compile_ops(root, resolve(root)))
        # Wave 0: the three domain roots; wave 1: their leaves.
        assert sorted(op.dn for op in waves[0]) == ["uni/fabric", "uni/infra", "uni/tn-p"]
        assert sorted(op.dn for op in waves[1]) == [
            "uni/fabric/time-t",
            "uni/infra/attentp-a",
            "uni/tn-p/ctx-v",
        ]


class TestAccessPoliciesGolden:
    """Act 2 — VLAN pool, physical domain, AAEP, interface + switch policies.

    Every reference is a ``bind()``: name flavor (``cdp``, ``link_level``),
    dn flavor (``aaep``, ``vlan_pool``, ``interface_profile``), abstract
    targets (``domain``, ``policy_group``), and a cross-domain edge (the EPG
    binds the phys-dom declared in the same design).
    """

    def test_payload(self) -> None:
        cfg = design()
        inf = cfg.infra()
        inf.vlan_pool("prod", "static").range("vlan-100", "vlan-199")
        cfg.phys_dom("prod-phys").bind(vlan_pool="prod")
        inf.aaep("prod-aaep").bind(domain="prod-phys")
        inf.cdp_policy("cdp-on", admin_state="enabled")
        inf.link_level_policy("10g", speed="10G")
        grp = inf.func_profile().access_group("server-pg")
        grp.bind(aaep="prod-aaep", cdp="cdp-on", link_level="10g")
        lp = inf.leaf_profile("leaf-01")
        lp.leaf_selector("leaf-01", "range").node_block("blk1", from_node_id=101, to_node_id=101)
        lp.bind(interface_profile="leaf-01-ports")
        sel = inf.access_port_profile("leaf-01-ports").port_selector("1.01", "range")
        sel.port_block("blk1", from_port_id=1, to_port_id=1)
        sel.bind(policy_group="server-pg")
        t = cfg.tenant("prod")
        t.vrf("main")
        t.bd("web").bind(vrf="main")
        epg = t.app("app1").epg("web").bind(bd="web", domain="prod-phys")
        epg.static_path("topology/pod-1/paths-101/pathep-[eth1/1]", encap="vlan-100")

        assert cfg.to_payload() == _mo(
            "polUni",
            {},
            _mo(
                "infraInfra",
                {},
                _mo(
                    "fvnsVlanInstP",
                    {"name": "prod", "allocMode": "static"},
                    _mo("fvnsEncapBlk", {"from": "vlan-100", "to": "vlan-199"}),
                ),
                _mo(
                    "infraAttEntityP",
                    {"name": "prod-aaep"},
                    _mo("infraRsDomP", {"tDn": "uni/phys-prod-phys"}),
                ),
                _mo("cdpIfPol", {"name": "cdp-on", "adminSt": "enabled"}),
                _mo("fabricHIfPol", {"name": "10g", "speed": "10G"}),
                _mo(
                    "infraFuncP",
                    {},
                    _mo(
                        "infraAccPortGrp",
                        {"name": "server-pg"},
                        _mo("infraRsAttEntP", {"tDn": "uni/infra/attentp-prod-aaep"}),
                        _mo("infraRsCdpIfPol", {"tnCdpIfPolName": "cdp-on"}),
                        _mo("infraRsHIfPol", {"tnFabricHIfPolName": "10g"}),
                    ),
                ),
                _mo(
                    "infraNodeP",
                    {"name": "leaf-01"},
                    _mo(
                        "infraLeafS",
                        {"name": "leaf-01", "type": "range"},
                        _mo(
                            "infraNodeBlk",
                            {"name": "blk1", "from_": "101", "to_": "101"},
                        ),
                    ),
                    _mo("infraRsAccPortP", {"tDn": "uni/infra/accportprof-leaf-01-ports"}),
                ),
                _mo(
                    "infraAccPortP",
                    {"name": "leaf-01-ports"},
                    _mo(
                        "infraHPortS",
                        {"name": "1.01", "type": "range"},
                        _mo(
                            "infraPortBlk",
                            {"name": "blk1", "fromPort": "1", "toPort": "1"},
                        ),
                        _mo(
                            "infraRsAccBaseGrp",
                            {"tDn": "uni/infra/funcprof/accportgrp-server-pg"},
                        ),
                    ),
                ),
            ),
            _mo(
                "physDomP",
                {"name": "prod-phys"},
                _mo("infraRsVlanNs", {"tDn": "uni/infra/vlanns-[prod]-static"}),
            ),
            _mo(
                "fvTenant",
                {"name": "prod"},
                _mo("fvCtx", {"name": "main"}),
                _mo("fvBD", {"name": "web"}, _mo("fvRsCtx", {"tnFvCtxName": "main"})),
                _mo(
                    "fvAp",
                    {"name": "app1"},
                    _mo(
                        "fvAEPg",
                        {"name": "web"},
                        _mo(
                            "fvRsPathAtt",
                            {
                                "tDn": "topology/pod-1/paths-101/pathep-[eth1/1]",
                                "encap": "vlan-100",
                            },
                        ),
                        _mo("fvRsBd", {"tnFvBDName": "web"}),
                        _mo("fvRsDomAtt", {"tDn": "uni/phys-prod-phys"}),
                    ),
                ),
            ),
        )
