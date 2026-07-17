"""External connectivity — EIGRP autonomous systems and interfaces (non-prod).

Run:
    uv run pytest tests/integration/06_l3out/test_006_eigrp.py -m integration -s

EIGRP on an L3Out: the autonomous-system enabler, EIGRP interfaces (each riding an
SVI on a VLAN from the shared lane) with authentication driven by a tenant key
chain, and EIGRP interface policies swept across their control flags. The VRF
carries an EIGRP address-family context policy (both metric styles).

One VRF backs the L3Out; addresses use a 10.x scheme. Values are illustrative.
``wipe(aci)`` is operator-only.
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
L3OUT = "niwaki-it-l3o-eigrp"
VRF = "niwaki-it-l3o-eigrp-vrf"

# EIGRP interface-policy control-flag combinations.
IF_CONTROLS = ["split-horizon", "bfd", "nh-self", "passive"]


def _leaves(aci: Niwaki) -> list[tuple[str, int]]:
    """Discover the fabric's leaf switches at runtime — (name, node-id), sorted by id."""
    found: list[tuple[str, int]] = []
    for node in aci.query("fabricNode").fetch():
        data = node.model_dump(by_alias=True)
        if data.get("role") == "leaf" and data.get("name") and data.get("id"):
            found.append((data["name"], int(data["id"])))
    return sorted(found, key=lambda pair: pair[1])


def _scaffold(t: Cursor) -> None:
    t.infra().vlan_pool(POOL, "static").range(
        "vlan-2600", "vlan-2699", allocation_mode="static", role="external"
    )
    t.l3_dom(L3DOM).bind(vlan_pool=POOL)
    # EIGRP interface policies, one per control flag.
    for ctrl in IF_CONTROLS:
        t.eigrp_interface_policy(
            f"niwaki-it-eigrp-{ctrl}",
            interface_controls=ctrl,
            hello_interval=5,
            hold_interval=15,
            eigrp_interface_bandwidth=100000,
            description=f"EIGRP interface control {ctrl}.",
        )
    # EIGRP address-family context policies, both metric styles.
    for style in ("narrow", "wide"):
        t.eigrp_address_family_context_policy(
            f"niwaki-it-eigrp-af-{style}",
            metric_style=style,
            active_timer=3,
            external_distance=170,
            internal_distance=90,
            maximum_ecmp_paths=8,
            description=f"EIGRP AF context, {style} metrics.",
        )
    # A key chain with two key policies for EIGRP authentication.
    kc = t.tenant_keychain_policy("niwaki-it-eigrp-kc", description="EIGRP key chain.")
    kc.key_policy(1, pre_shared_key="niwaki-eigrp-key-1", description="Key 1.")
    kc.key_policy(2, pre_shared_key="niwaki-eigrp-key-2", description="Key 2.")


def test_eigrp_interfaces(live_aci: Niwaki) -> None:
    """EIGRP autonomous system with authenticated interfaces over the control-flag set."""
    t = tenant(TN)
    _scaffold(t)
    leaves = _leaves(live_aci)

    t.vrf(VRF, description="VRF for the EIGRP L3Out.").bind(
        eigrp_address_family="niwaki-it-eigrp-af-wide"
    )
    out = t.l3out(L3OUT, description="EIGRP L3Out.").bind(vrf=VRF).bind(domain=L3DOM)
    out.eigrp(autonomous_system_number=100, description="EIGRP AS 100.")

    for lidx, (lname, node_id) in enumerate(leaves, start=1):
        np = out.node_profile(f"np-{lname}")
        np.node_attachment(
            f"topology/pod-1/node-{node_id}", rtr_id=f"10.6.0.{lidx}", rtr_id_loop_back=False
        )
        # eigrp_interface is a singleton per interface profile, so each control
        # policy rides its own interface profile (one SVI, one EIGRP interface).
        for k, ctrl in enumerate(IF_CONTROLS):
            port = 40 + k
            ifp = np.interface_profile(f"if-{lname}-{ctrl}", description=f"EIGRP {ctrl} profile.")
            ifp.path_attachment(
                f"topology/pod-1/paths-{node_id}/pathep-[eth1/{port}]",
                if_inst_t="ext-svi",
                addr=f"10.6.{port}.{lidx}/24",
                encap=f"vlan-{2670 + k}",
                mode="regular",
            )
            eigrp_if = ifp.eigrp_interface(description=f"EIGRP interface, control {ctrl}.")
            eigrp_if.bind(eigrp_interface_policy=f"niwaki-it-eigrp-{ctrl}")
            eigrp_if.eigrp_authentication().bind(keychain_policy="niwaki-it-eigrp-kc")

    t.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — operator only; removes this file's L3Out and VRF."""
    for dn in (f"uni/tn-{TN}/out-{L3OUT}", f"uni/tn-{TN}/ctx-{VRF}"):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
