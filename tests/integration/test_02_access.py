"""Act 2 — access policies: from interface policies to switch profiles.

The complete cabling chain every ACI engineer recites, bottom-up:

    interface policies → VLAN pool → domains → AAEP
        → interface policy groups (access / port-channel / VPC)
        → interface profiles (named after their switch profile; one
          selector per port, 1.01-1.58 — eth1/59-60 reserved)
        → switch profiles (leaf-01, leaf-02, spine-01)
        → VPC protection group (101 ⇄ 102)

The whole chain is **one design**: makers declare the structure,
``bind()`` declares every reference (name flavor for interface policies, dn
flavor for AAEP/domains/profiles, abstract targets for ``policy_group`` and
``domain``), and the VPC pair rides in the same design — ``fabric()`` is one
implicit-pop maker away.  One strict push ships everything atomically; the
day-2 policy flip then lands as a declarative set+push with a ``plan``
audit before and after.

Requires act 1 (registered leaves).  Cleanup happens at the end of act 3.
"""

from __future__ import annotations

import pytest

from niwaki import Niwaki
from niwaki.design import Cursor, PlanResult, infra
from tests.integration.conftest import (
    AAEP,
    ACCESS_PG,
    IFPROF_LEAF1,
    IFPROF_LEAF2,
    L3_DOM,
    LEAF1_ID,
    LEAF1_NAME,
    LEAF2_ID,
    LEAF2_NAME,
    PC_PG,
    PHYS_DOM,
    SPINE1_ID,
    SPINE1_NAME,
    VLAN_POOL,
    VPC_PAIR_NAME,
    VPC_PG,
)

pytestmark = pytest.mark.integration


def access_design() -> Cursor:
    """The full act-2 cabling chain — one fluent declaration.

    Implicit pop carries the chain across levels and even across domains
    (``.phys_dom()`` climbs to ``uni``, ``.fabric()`` opens a sibling
    domain).  References are ``bind()`` everywhere — name flavor for the
    interface policies, dn flavor for AAEP/profiles, abstract targets for
    ``domain`` and ``policy_group`` — and the AAEP→domain bind is a forward
    reference (the phys-dom is declared further down).
    """
    inf = infra()
    cfg = (
        inf
        # ── 1 · Interface policies: the reusable bricks ──────────────────────
        .cdp_policy("niwaki-cdp-on", admin_state="enabled")
        .lldp_policy("niwaki-lldp-on", receive_state="enabled", transmit_state="enabled")
        .lacp_policy("niwaki-lacp-active", mode="active")
        .link_level_policy("niwaki-10g", speed="10G")
        .mcp_policy("niwaki-mcp-on", admin_state="enabled")
        .stp_policy("niwaki-bpdu-guard", controls="bpdu-guard")
        .storm_control_policy("niwaki-storm",
                              broadcast_traffic_rate=80.0, multicast_traffic_rate=80.0)
        # ── 2 · VLAN pool + AAEP (domain bound by forward reference) ─────────
        .vlan_pool(VLAN_POOL, "static", description="niwaki walkthrough pool")
            .range("vlan-200", "vlan-249", allocation_mode="static")
            .range("vlan-250", "vlan-299", allocation_mode="static")
        .aaep(AAEP, description="niwaki walkthrough AAEP").bind(domain=PHYS_DOM)
        # ── 3 · Interface policy groups ───────────────────────────────────────
        .func_profile()
            .access_group(ACCESS_PG, description="single-homed host port")
                .bind(cdp="niwaki-cdp-on", aaep=AAEP)
            .port_channel(PC_PG, link_aggregation_type="link",
                          description="LACP port-channel")
                .bind(lacp="niwaki-lacp-active", aaep=AAEP)
            .port_channel(VPC_PG, link_aggregation_type="node")
                .bind(cdp="niwaki-cdp-on", lldp="niwaki-lldp-on",
                      lacp="niwaki-lacp-active", link_level="niwaki-10g",
                      stp="niwaki-bpdu-guard", aaep=AAEP)
        # ── 4 · Interface profiles — built with loops below (one selector
        #        per port, owner's convention; the chain is never mandatory) ──
        # ── 5 · Switch profiles: leaf-01 / leaf-02 / spine-01 ─────────────────
        .leaf_profile(LEAF1_NAME, description=f"switch profile for {LEAF1_NAME}")
            .bind(interface_profile=IFPROF_LEAF1)
            .leaf_selector(LEAF1_NAME, "range")
                .node_block(f"blk-{LEAF1_ID}", from_node_id=LEAF1_ID, to_node_id=LEAF1_ID)
        .leaf_profile(LEAF2_NAME, description=f"switch profile for {LEAF2_NAME}")
            .bind(interface_profile=IFPROF_LEAF2)
            .leaf_selector(LEAF2_NAME, "range")
                .node_block(f"blk-{LEAF2_ID}", from_node_id=LEAF2_ID, to_node_id=LEAF2_ID)
        .spine_profile(SPINE1_NAME, description="spine switch profile")
            .spine_selector(SPINE1_NAME, "range")
                .node_block(f"blk-{SPINE1_ID}", from_node_id=SPINE1_ID, to_node_id=SPINE1_ID)
        # ── 6 · Domains under uni, bound to the pool ──────────────────────────
        .phys_dom(PHYS_DOM).bind(vlan_pool=VLAN_POOL)
        .l3_dom(L3_DOM).bind(vlan_pool=VLAN_POOL)
        # ── 7 · vPC protection: a sibling fabric domain, one maker away ───────
        .fabric()
            .vpc_protection()
                .vpc_pair(VPC_PAIR_NAME, logical_pair_id=10)
                    .node(LEAF1_ID)
                    .node(LEAF2_ID)
    )  # fmt: skip

    # Owner's convention: each leaf interface profile carries the SAME name as
    # its switch profile, and holds one selector per front-panel port, named
    # 1.01-1.58.  eth1/59-60 are reserved — no selector, ever.  ESXi vPC owns
    # 1.10/1.11; every other port carries the single-host access group.
    for prof_name in (IFPROF_LEAF1, IFPROF_LEAF2):
        prof = inf.access_port_profile(prof_name, description=f"interface profile for {prof_name}")
        for port in range(1, 59):
            selector = prof.port_selector(f"1.{port:02d}", "range")
            selector.bind(policy_group=VPC_PG if port in (10, 11) else ACCESS_PG)
            selector.port_block(f"blk-{port}", from_port_id=port, to_port_id=port)

    return cfg


class Test2AccessPolicies:
    """Day-1: make the front-panel ports usable by tenants."""

    # ── 1 · One design, one atomic push ──────────────────────────────────────

    def test_01_plan_then_strict_push(self, live_aci: Niwaki) -> None:
        cfg = access_design()

        plan = cfg.push(live_aci, mode="plan")
        assert isinstance(plan, PlanResult)
        assert plan.has_changes
        assert f"uni/infra/attentp-{AAEP}" in plan.creates

        report = cfg.push(live_aci, mode="strict")
        assert report.request_count == 1  # the whole cabling chain, atomically

    def test_02_replan_is_idempotent(self, live_aci: Niwaki) -> None:
        assert not access_design().push(live_aci, mode="plan").has_changes

    # ── 2 · Audit: read the chain back like the GUI tree ─────────────────────

    def test_03_audit_policies_and_pool(self, live_aci: Niwaki) -> None:
        inf = live_aci.root.infra()
        assert inf.cdp_policy("niwaki-cdp-on").read().admin_state == "enabled"
        assert inf.lacp_policy("niwaki-lacp-active").read().mode == "active"
        assert inf.link_level_policy("niwaki-10g").read().speed == "10G"
        assert inf.storm_control_interface_policy("niwaki-storm").read()

        pool = inf.vlan_pool(name=VLAN_POOL, allocation_mode="static")
        assert pool.query("fvnsEncapBlk").count() == 2
        assert inf.aaep(AAEP).query("infraRsDomP").count() == 1

    def test_04_audit_policy_groups(self, live_aci: Niwaki) -> None:
        func = live_aci.root.infra().func_profile()
        vpc = func.port_channel(VPC_PG)
        assert vpc.read().link_aggregation_type == "node"
        assert vpc.query("infraRsLacpPol").count() == 1
        assert func.leaf_access_port_policy_group(ACCESS_PG).query("infraRsAttEntP").count() == 1

    def test_05_audit_profiles(self, live_aci: Niwaki) -> None:
        inf = live_aci.root.infra()
        prof = inf.access_port_profile(IFPROF_LEAF1)

        selectors = prof.query("infraHPortS").fetch()
        assert sorted(s.name for s in selectors) == [f"1.{p:02d}" for p in range(1, 59)]

        blocks = prof.query("infraPortBlk").fetch()
        assert len(blocks) == 58
        assert all(b.from_port_id == b.to_port_id for b in blocks)
        # eth1/59-60 are reserved: ports 1-58 covered exactly once, no more.
        assert sorted(int(b.from_port_id) for b in blocks) == list(range(1, 59))

        # ESXi vPC owns 1.10/1.11; the other 56 carry the access group.
        rs = prof.query("infraRsAccBaseGrp").fetch()
        vpc_dn = f"uni/infra/funcprof/accbundle-{VPC_PG}"
        assert sum(1 for r in rs if r.target_dn == vpc_dn) == 2
        assert len(rs) == 58

        leaf = inf.leaf_profile(LEAF1_NAME)
        assert leaf.query("infraNodeBlk").first() is not None
        assert leaf.query("infraRsAccPortP").count() == 1
        assert inf.spine_profile(SPINE1_NAME).query("infraNodeBlk").count() == 1

    def test_06_audit_vpc_domain(self, live_aci: Niwaki) -> None:
        protpol = live_aci.root.fabric().virtual_port_channel_security_policy()
        pair = protpol.vpc_explicit_protection_group(VPC_PAIR_NAME)
        assert pair.query("fabricNodePEp").count() == 2

    # ── 3 · Day-2: flip a policy declaratively ───────────────────────────────

    def test_07_day2_flip_is_declarative(self, live_aci: Niwaki) -> None:
        """Declare the new desired state; carriers ride as bare upserts."""
        flip = infra().cdp_policy("niwaki-cdp-on", admin_state="disabled")

        plan = flip.push(live_aci, mode="plan")
        assert plan.updates == {
            "uni/infra/cdpIfP-niwaki-cdp-on": {"admin_state": ("enabled", "disabled")}
        }
        assert "uni/infra" in plan.unchanged  # the carrier is never a change

        flip.push(live_aci)
        assert live_aci.root.infra().cdp_policy("niwaki-cdp-on").read().admin_state == "disabled"

        # Converge back and prove it with a clean plan.
        back = infra().cdp_policy("niwaki-cdp-on", admin_state="enabled")
        back.push(live_aci)
        assert not back.push(live_aci, mode="plan").has_changes
