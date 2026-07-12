"""Golden tests — the two founding brainstorm mockups, verbatim.

These snippets were the acceptance criterion of the design DSL v1: they are
the owner-written target syntax, and they must build, validate closed-world,
and compile to the expected APIC structures.  Only the corrections agreed
during the brainstorm are applied: Python literals, quoted names, ``.pim()``
instead of ``set(multicast=True)``, subnets as child objects.
"""

from __future__ import annotations

from typing import Any

from niwaki.design import Cursor, tenant
from tests.design.conftest import find_child


def _mockup_networking() -> Cursor:
    """Mockup 1 — tenants/apps/EPGs/BDs/VRFs/L3Outs."""
    return (
        tenant("prod")
        .app("prod")
            .epg("prod-frontend").bind(bd="prod-frontend")
            .epg("prod-backend").bind(bd="prod-backend")
            .epg("prod-database").bind(bd="prod-database")
        .app("post-prod")
            .epg("post-prod-frontend").bind(bd="post-prod-frontend")
            .epg("post-prod-backend").bind(bd="post-prod-backend")
            .epg("post-prod-database").bind(bd="post-prod-database")
        .bd("prod-frontend")
            .set(unicast_routing=True, multicast_allow=True)
            .subnet("10.0.1.1/24")
            .bind(vrf="prod")
        .bd("prod-backend")
            .set(unicast_routing=True, multicast_allow=True)
            .subnet("10.0.2.1/24")
            .bind(vrf="prod")
        .bd("prod-database")
            .set(unicast_routing=True, multicast_allow=True)
            .subnet("10.0.3.1/24")
            .bind(vrf="prod")
        .bd("post-prod-frontend")
            .set(unicast_routing=True, multicast_allow=True)
            .subnet("10.0.4.1/24")
            .bind(vrf="post-prod")
        .bd("post-prod-backend")
            .set(unicast_routing=True, multicast_allow=True)
            .subnet("10.0.10.1/24")
            .bind(vrf="post-prod")
        .bd("post-prod-database")
            .set(unicast_routing=False, multicast_allow=True)
            .bind(vrf="post-prod")
        .vrf("prod")
            .pim()
            .bind(l3out="prod")
        .vrf("post-prod")
            .bind(l3out="post-prod")
        .l3out("prod")
        .l3out("post-prod")
    )  # fmt: skip


def _mockup_contracts() -> Cursor:
    """Mockup 2 — filters/contracts/subjects + provide/consume wiring."""
    return (
        tenant("prod")
        .filter("web")
            .entry("http", tcp=80)
            .entry("https", tcp=443)
        .filter("api")
            .entry("rest", tcp=8080)
        .filter("db")
            .entry("postgres", tcp=5432)
        .filter("icmp")
            .entry("ping", protocol="icmp")
        .contract("fe-to-be")
            .set(scope="vrf")
            .subject("api")
                .bind(filter="api")
                .bind(filter="icmp")
        .contract("be-to-db")
            .subject("sql")
                .bind(filter="db")
        .app("prod")
            .epg("prod-frontend")
                .bind(bd="prod-frontend")
                .consume("fe-to-be")
            .epg("prod-backend")
                .bind(bd="prod-backend")
                .provide("fe-to-be")
                .consume("be-to-db")
            .epg("prod-database")
                .bind(bd="prod-database")
                .provide("be-to-db")
        .bd("prod-frontend").bind(vrf="prod")
        .bd("prod-backend").bind(vrf="prod")
        .bd("prod-database").bind(vrf="prod")
        .vrf("prod")
    )  # fmt: skip


def _tenant_env(design: Cursor) -> dict[str, Any]:
    (env,) = design.to_payload()["polUni"]["children"]
    return env


class TestNetworkingMockup:
    def test_builds_and_validates_closed_world(self) -> None:
        env = _tenant_env(_mockup_networking())
        # 2 apps + 6 BDs + 2 VRFs + 2 L3Outs under the tenant.
        assert len(env["fvTenant"]["children"]) == 12

    def test_epgs_bound_to_their_bds(self) -> None:
        env = _tenant_env(_mockup_networking())
        app = find_child(env, "fvAp", name="prod")
        epg = find_child(app, "fvAEPg", name="prod-frontend")
        rsbd = find_child(epg, "fvRsBd")
        assert rsbd["fvRsBd"]["attributes"]["tnFvBDName"] == "prod-frontend"

    def test_bd_carries_subnet_and_vrf_binding(self) -> None:
        env = _tenant_env(_mockup_networking())
        bd = find_child(env, "fvBD", name="prod-backend")
        assert bd["fvBD"]["attributes"]["unicastRoute"] == "true"
        find_child(bd, "fvSubnet", ip="10.0.2.1/24")
        rsctx = find_child(bd, "fvRsCtx")
        assert rsctx["fvRsCtx"]["attributes"]["tnFvCtxName"] == "prod"

    def test_l2_bd_has_no_subnet(self) -> None:
        env = _tenant_env(_mockup_networking())
        bd = find_child(env, "fvBD", name="post-prod-database")
        assert bd["fvBD"]["attributes"]["unicastRoute"] == "false"
        children = bd["fvBD"].get("children", [])
        assert all("fvSubnet" not in c for c in children)

    def test_vrf_has_pim_child_and_inverse_l3out_binding(self) -> None:
        env = _tenant_env(_mockup_networking())
        vrf = find_child(env, "fvCtx", name="prod")
        find_child(vrf, "pimCtxP")
        # The vrf.bind(l3out=...) landed on the L3Out side (inverse edge).
        l3out = find_child(env, "l3extOut", name="prod")
        rsectx = find_child(l3out, "l3extRsEctx")
        assert rsectx["l3extRsEctx"]["attributes"]["tnFvCtxName"] == "prod"


class TestContractsMockup:
    def test_builds_and_validates_closed_world(self) -> None:
        env = _tenant_env(_mockup_contracts())
        # 4 filters + 2 contracts + 1 app + 3 BDs + 1 VRF.
        assert len(env["fvTenant"]["children"]) == 11

    def test_entry_sugar_compiles_to_wire_fields(self) -> None:
        env = _tenant_env(_mockup_contracts())
        flt = find_child(env, "vzFilter", name="api")
        entry = find_child(flt, "vzEntry", name="rest")
        attrs = entry["vzEntry"]["attributes"]
        assert attrs["etherT"] == "ip"
        assert attrs["prot"] == "tcp"
        assert attrs["dFromPort"] == "8080"
        assert attrs["dToPort"] == "8080"

    def test_scope_vrf_translated_to_context(self) -> None:
        env = _tenant_env(_mockup_contracts())
        contract = find_child(env, "vzBrCP", name="fe-to-be")
        assert contract["vzBrCP"]["attributes"]["scope"] == "context"

    def test_subject_filter_attachments(self) -> None:
        env = _tenant_env(_mockup_contracts())
        contract = find_child(env, "vzBrCP", name="fe-to-be")
        subject = find_child(contract, "vzSubj", name="api")
        names = {
            c["vzRsSubjFiltAtt"]["attributes"]["tnVzFilterName"]
            for c in subject["vzSubj"]["children"]
        }
        assert names == {"api", "icmp"}

    def test_provide_consume_wiring(self) -> None:
        env = _tenant_env(_mockup_contracts())
        app = find_child(env, "fvAp", name="prod")
        backend = find_child(app, "fvAEPg", name="prod-backend")
        prov = find_child(backend, "fvRsProv")
        cons = find_child(backend, "fvRsCons")
        assert prov["fvRsProv"]["attributes"]["tnVzBrCPName"] == "fe-to-be"
        assert cons["fvRsCons"]["attributes"]["tnVzBrCPName"] == "be-to-db"
