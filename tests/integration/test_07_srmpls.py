"""Act 7 — SR-MPLS handoff: the infra transport and the tenant that rides it.

Segment Routing MPLS in ACI is a two-sided story, and this act tells both
against the live APIC:

1. **The SR-MPLS Infra L3Out** (tenant ``infra``, VRF ``overlay-1``): the
   fabric's handoff to the DC-PE.  Two border leaves, each with a transport
   loopback and a **node SID** (the Segment Identifier — ``srgb_index`` + the
   data-plane loopback); two routed interfaces, one per leaf; the label
   policies (interface, the ``default`` global config with its **SRGB** label
   range, custom QoS); a BGP-EVPN session to the DC-PE carrying the
   ``sr-mpls`` peer flag; and a **provider label** the tenants consume.

2. **A user tenant SR-MPLS L3Out**: it references the infra transport by
   **consuming that provider label** (matched by name, owner ``infra``) and
   publishes an external EPG for ``0.0.0.0/0``.

The SR-MPLS vocabulary (``mplsExtP``, ``mplsNodeSidP``, ``mplsSrgbLabelPol``,
``mplsIfP``/``mplsIfPol``, ``mplsLabelPol``, ``l3extProvLbl``/``l3extConsLbl``,
``bgpInfraPeerP``) is the live gate here.  ``overlay-1`` is declared only to
satisfy the closed world — a no-op upsert that never touches the system VRF.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import Cursor, tenant
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

# ── Infra side (lives under the fabric's own ``infra`` tenant) ─────────────────
INFRA_L3OUT = "niwaki-srmpls"
INFRA_L3OUT_DN = f"uni/tn-infra/out-{INFRA_L3OUT}"
MPLS_IF_POL = "niwaki-srmpls"
MPLS_QOS = "niwaki-srmpls"
PROVIDER_LABEL = "niwaki-srmpls"

NODE1_DN = "topology/pod-1/node-101"
NODE2_DN = "topology/pod-1/node-102"
PATH1_DN = "topology/pod-1/paths-101/pathep-[eth1/33]"
PATH2_DN = "topology/pod-1/paths-102/pathep-[eth1/33]"

# ── User side ─────────────────────────────────────────────────────────────────
USER_TENANT = "niwaki-srmpls-user"
USER_L3OUT_DN = f"uni/tn-{USER_TENANT}/out-srmpls"
RT_VALUE = "route-target:as2-nn2:100:100"


def _wipe(aci: Niwaki) -> None:
    """Delete only what this act owns — never ``infra`` or ``overlay-1``.

    The infra pieces hang directly off the shared ``infra`` tenant, so each
    one is removed by DN; the user side is a whole tenant.  The ``default``
    MPLS label policy is a protected system singleton (the APIC refuses to
    delete it), so it is left in place — re-declaring its SRGB is a no-op.
    """
    for dn in (
        INFRA_L3OUT_DN,
        f"uni/tn-infra/mplsifpol-{MPLS_IF_POL}",
        f"uni/tn-infra/qosmplscustom-{MPLS_QOS}",
        f"uni/tn-{USER_TENANT}",
    ):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()


def infra_srmpls_design() -> Cursor:
    """The SR-MPLS Infra L3Out: the fabric's transport handoff to the DC-PE."""
    infra = tenant("infra")

    # Tenant-level MPLS policies the L3Out pieces reference.
    infra.mpls_interface_policy(MPLS_IF_POL)
    label_pol = infra.mpls_global_configuration("default")  # singleton: only "default"
    label_pol.srgb(1, srgb_minimum_label=16000, srgb_maximum_label=23999)
    infra.mpls_custom_qos_policy(MPLS_QOS)

    # overlay-1 already exists; declaring it satisfies the closed world (no
    # attributes → a no-op upsert that never modifies the system VRF).
    infra.vrf("overlay-1")

    l3out = infra.l3out(INFRA_L3OUT).bind(vrf="overlay-1")
    l3out.mpls_external().bind(mpls_global_configuration="default")
    l3out.provider_label(PROVIDER_LABEL)

    nodes = l3out.node_profile("border-leaves")
    nodes.bind(mpls_custom_qos_policy=MPLS_QOS)

    leaf1 = nodes.node_attachment(NODE1_DN, rtr_id="10.10.10.101")
    leaf1.loopback("10.10.10.101").node_sid(1, loopback_addr="20.20.20.101")
    leaf2 = nodes.node_attachment(NODE2_DN, rtr_id="10.10.10.102")
    leaf2.loopback("10.10.10.102").node_sid(2, loopback_addr="20.20.20.102")

    # BGP-EVPN session to the DC-PE — the peer must carry the sr-mpls flag.
    peer = nodes.infra_peer_connectivity_profile(
        "50.0.0.100", peer_type="sr-mpls", ebgp_multihop_ttl_value=10
    )
    peer.autonomous_system_profile(autonomous_system_number=65001)

    interfaces = nodes.interface_profile("uplinks")
    interfaces.path_attachment(
        PATH1_DN, if_inst_t="sub-interface", addr="192.0.2.1/30", encap="vlan-4000"
    )
    interfaces.path_attachment(
        PATH2_DN, if_inst_t="sub-interface", addr="192.0.2.5/30", encap="vlan-4000"
    )
    interfaces.mpls_interface().bind(mpls_interface_policy=MPLS_IF_POL)

    return infra


def user_srmpls_design() -> Cursor:
    """A user tenant that rides the infra transport by consuming its label.

    The label alone is not enough in production: the tenant's VRF needs the BGP
    route targets that map it into the MPLS-VPN, and the L3Out needs the
    import/export route maps that permit the prefixes across the hand-off.
    """
    user = tenant(USER_TENANT, description="niwaki walkthrough act 7")

    vrf = user.vrf("prod")
    route_targets = vrf.route_target_profile("ipv4-ucast")
    route_targets.route_target(RT_VALUE, "import")
    route_targets.route_target(RT_VALUE, "export")

    # mpls_enabled is what makes this a SR-MPLS VRF L3Out, not a classic one.
    l3out = user.l3out("srmpls", mpls_enabled=True).bind(vrf="prod")
    # Consume the infra provider label (matched by name, owned by infra).
    l3out.consumer_label(PROVIDER_LABEL, represents_the_provider_label_ownership="infra")

    # The import/export route maps that permit the prefixes (the reserved
    # names make them the L3Out's default import/export policies).
    for direction in ("default-import", "default-export"):
        l3out.route_control_profile(direction).route_control_context(
            "permit-all", action="permit", local_order=1
        )

    external = l3out.external_epg("all")
    external.subnet("0.0.0.0/0")
    return user


class Test7SrMpls:
    """SR-MPLS live gate: infra transport, then the tenant that references it."""

    def test_01_plan_then_push_infra(self, live_aci: Niwaki) -> None:
        _wipe(live_aci)
        design = infra_srmpls_design()

        plan = design.push(live_aci, mode="plan")
        assert plan.has_changes
        assert INFRA_L3OUT_DN in plan.creates
        # Declaring overlay-1 is a pure no-op — the system VRF is never touched.
        assert not any("ctx-overlay-1" in dn for dn in plan.updates)

        design.push(live_aci, mode="staged")

    def test_02_infra_replan_is_converged(self, live_aci: Niwaki) -> None:
        assert infra_srmpls_design().push(live_aci, mode="plan").has_changes is False

    def test_03_the_fabric_confirms_the_transport(self, live_aci: Niwaki) -> None:
        infra = live_aci.tenant("infra")

        # The two node SIDs — the Segment Identifiers of the border leaves.
        sids = infra.query("mplsNodeSidP").under(INFRA_L3OUT_DN).fetch()
        assert sorted(s.srgb_index for s in sids) == [1, 2]

        # The SRGB — the segment-label range on the default global config.
        (srgb,) = infra.query("mplsSrgbLabelPol").fetch()
        assert (srgb.srgb_minimum_label, srgb.srgb_maximum_label) == (16000, 23999)

        # Two routed interfaces, one per leaf.
        paths = infra.query("l3extRsPathL3OutAtt").under(INFRA_L3OUT_DN).fetch()
        assert sorted(p.addr for p in paths) == ["192.0.2.1/30", "192.0.2.5/30"]

        # The BGP-EVPN handoff peer, carrying the sr-mpls flag.
        (peer,) = infra.query("bgpInfraPeerP").under(INFRA_L3OUT_DN).fetch()
        assert peer.peer_address == "50.0.0.100"
        assert "sr-mpls" in peer.peer_type

        # The provider label tenants will consume.
        (prov,) = infra.query("l3extProvLbl").under(INFRA_L3OUT_DN).fetch()
        assert prov.name == PROVIDER_LABEL

    def test_04_push_user_and_replan_converges(self, live_aci: Niwaki) -> None:
        design = user_srmpls_design()

        plan = design.push(live_aci, mode="plan")
        assert plan.has_changes
        assert USER_L3OUT_DN in plan.creates

        design.push(live_aci, mode="staged")
        assert user_srmpls_design().push(live_aci, mode="plan").has_changes is False

    def test_05_the_user_references_the_infra(self, live_aci: Niwaki) -> None:
        user = live_aci.tenant(USER_TENANT)

        # It is a SR-MPLS VRF L3Out (mplsEnabled), not a classic L3Out.
        (out,) = user.query("l3extOut").fetch()
        assert out.mpls_enabled is True

        # The consumer label names the infra provider label — the reference.
        (cons,) = user.query("l3extConsLbl").under(USER_L3OUT_DN).fetch()
        assert cons.name == PROVIDER_LABEL

        subnets = user.query("l3extSubnet").under(USER_L3OUT_DN).fetch()
        assert [s.subnet for s in subnets] == ["0.0.0.0/0"]  # D2: wire "ip" → subnet

        # The route targets that map the VRF into the MPLS-VPN (import + export).
        route_targets = user.query("bgpRtTarget").fetch()
        assert {(rt.route_target, rt.type) for rt in route_targets} == {
            (RT_VALUE, "import"),
            (RT_VALUE, "export"),
        }

        # The import/export route maps permitting the prefixes.
        maps = user.query("rtctrlProfile").under(USER_L3OUT_DN).fetch()
        assert sorted(m.name for m in maps) == ["default-export", "default-import"]

    def test_06_the_fabric_raises_no_faults(self, live_aci: Niwaki) -> None:
        for node, scope in (
            (live_aci.tenant("infra"), INFRA_L3OUT_DN),
            (live_aci.tenant(USER_TENANT), USER_L3OUT_DN),
        ):
            faults = node.query("faultInst").under(scope).fetch()
            blocking = [f for f in faults if f.severity in {"critical", "major"}]
            assert not blocking, [f"{f.rule}: {f.descr}" for f in blocking]

    # No end-of-run cleanup: both L3Outs stay on the simulator for manual
    # investigation.  test_01 wipes at the START, keeping re-runs deterministic
    # and never touching the infra tenant or overlay-1.
