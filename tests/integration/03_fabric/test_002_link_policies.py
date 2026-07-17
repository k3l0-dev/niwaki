"""Fabric — LLDP and link/interface protocol policies (exhaustive combos, non-prod).

Run:
    uv run pytest tests/integration/03_fabric/test_002_link_policies.py -m integration -s

The port-level protocol policies the fabric interface policy-groups draw from:
LLDP (full cartesian of receive / transmit / DCBX-version), fabric link-level
(debounce spread), link-flap (flap thresholds), L3-interface (BFD-for-IS-IS both
ways), L2 MTU (fabric / management MTU spread), the fabric VXLAN policy (every
named UDP port plus numeric ports) and fabric node control (every feature
selection crossed with the control bitmask).

Exhaustive combination coverage, illustrative values — not a real fabric config.

``wipe(aci)`` (operator-only) removes every ``niwaki-it-*`` policy created here.
"""

from __future__ import annotations

import contextlib
import itertools

import pytest

from niwaki import Niwaki
from niwaki.design import fabric
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

LLDP = "niwaki-it-lldp"
FLINK = "niwaki-it-flink"
FLAP = "niwaki-it-linkflap"
L3IF = "niwaki-it-l3if"
MTU = "niwaki-it-mtu"
VXLAN = "niwaki-it-vxlan"
NODECTL = "niwaki-it-nodectl"

RX_STATES = ("enabled", "disabled")
TX_STATES = ("enabled", "disabled")
DCBX_VERSIONS = ("CEE", "IEEE")
DEBOUNCE_MS = (0, 100, 1000, 5000)
# max flaps 2..30, window seconds 5..420 by the schema.
FLAP_SPECS = ((2, 30), (5, 120), (10, 300), (30, 420))
MTU_SPECS = ((9000, 9000), (1500, 1500), (9216, 1500), (2240, 9000))
# Fabric VXLAN UDP port — a named-number field (name or numeric value).
VXLAN_PORTS: tuple[str | int, ...] = (
    "dns",
    "ftpData",
    "http",
    "https",
    "pop3",
    "rtsp",
    "smtp",
    "ssh",
    "unspecified",
    48879,
    4789,
)
# "mixed" (netflow+telemetry combined) is rejected by the APIC in this release.
NODE_FEATURES = ("analytics", "netflow", "telemetry")
NODE_BITMASKS = ("None", "Dom")


def test_lldp_policies(live_aci: Niwaki) -> None:
    fab = fabric()
    for rx, tx, dcbx in itertools.product(RX_STATES, TX_STATES, DCBX_VERSIONS):
        fab.lldp_policy(
            f"{LLDP}-{rx[:3]}-{tx[:3]}-{dcbx.lower()}",
            description=f"LLDP rx {rx}, tx {tx}, DCBX {dcbx}.",
            receive_state=rx,
            transmit_state=tx,
            dcbxp_version=dcbx,
        )
    fab.push(live_aci)


def test_link_level_and_flap(live_aci: Niwaki) -> None:
    fab = fabric()
    for debounce in DEBOUNCE_MS:
        fab.fabric_link_level_policy(
            f"{FLINK}-{debounce}",
            description=f"Fabric link-level policy, {debounce} ms debounce.",
            fabric_link_debounce_interval_msec=debounce,
        )
    for max_flaps, window in FLAP_SPECS:
        fab.fabric_link_flap_policy(
            f"{FLAP}-{max_flaps}-{window}",
            description=f"Link-flap: err-disable after {max_flaps} flaps in {window} s.",
            max_flaps_allowed_per_time=max_flaps,
            time_allowed_for_max_flaps=window,
        )
    fab.push(live_aci)


def test_l3_and_mtu(live_aci: Niwaki) -> None:
    fab = fabric()
    for bfd in ("enabled", "disabled"):
        fab.l3_interface_policy(
            f"{L3IF}-{bfd}",
            description=f"L3 interface policy, BFD-for-IS-IS {bfd}.",
            bfd_isis_policy_configuration=bfd,
        )
    for fabric_mtu, mgmt_mtu in MTU_SPECS:
        fab.fabric_l2_mtu_policy(
            f"{MTU}-{fabric_mtu}-{mgmt_mtu}",
            description=f"L2 MTU: {fabric_mtu} B fabric, {mgmt_mtu} B management.",
            mtu_size_for_fabric_ports=fabric_mtu,
            mtu_size_for_management_ports=mgmt_mtu,
        )
    fab.push(live_aci)


def test_vxlan_and_node_control(live_aci: Niwaki) -> None:
    fab = fabric()
    for port in VXLAN_PORTS:
        fab.fabric_vxlan_policy(
            f"{VXLAN}-{port}",
            description=f"Fabric VXLAN policy, UDP port {port}.",
            udp_port=port,
        )
    for feature, bitmask in itertools.product(NODE_FEATURES, NODE_BITMASKS):
        fab.fabric_node_control(
            f"{NODECTL}-{feature}-{bitmask.lower()}",
            description=f"Fabric node control: {feature} feature, {bitmask} bitmask.",
            feature_selection=feature,
            fabric_node_control_bitmask=bitmask,
        )
    fab.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    dns: list[str] = []
    for rx, tx, dcbx in itertools.product(RX_STATES, TX_STATES, DCBX_VERSIONS):
        dns.append(f"uni/fabric/lldpIfP-{LLDP}-{rx[:3]}-{tx[:3]}-{dcbx.lower()}")
    dns += [f"uni/fabric/fintfpol-{FLINK}-{d}" for d in DEBOUNCE_MS]
    dns += [f"uni/fabric/flinkflappol-{FLAP}-{m}-{w}" for m, w in FLAP_SPECS]
    dns += [f"uni/fabric/l3IfP-{L3IF}-{b}" for b in ("enabled", "disabled")]
    dns += [f"uni/fabric/l2pol-{MTU}-{f}-{m}" for f, m in MTU_SPECS]
    dns += [f"uni/fabric/vxlanpol-{VXLAN}-{p}" for p in VXLAN_PORTS]
    for feature, bitmask in itertools.product(NODE_FEATURES, NODE_BITMASKS):
        dns.append(f"uni/fabric/nodecontrol-{NODECTL}-{feature}-{bitmask.lower()}")
    for dn in dns:
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
