"""Act 1 — fabric bring-up: node registration and fabric-wide policies.

The APIC simulator starts with switches *discovered* (visible as ``dhcpClient``
entries) but not *registered*: no node IDs, no names, no fabric.  This act does
what an engineer does on day 0:

1. **Discover** the switches — a query on ``dhcpClient``, an operational class
   that is not in the generated set (the string-class query path).  Discovery
   is observation: the polling loop stays imperative by design.
2. **Register** them — each registration is a mini design pushed through
   ``controller()``: the ``ctrlrInst``/``fabricNodeIdentPol`` carriers ride
   along as attribute-less upserts (ADR-001 D-1).
3. **Fabric policies** — NTP, DNS, BGP (fabric ASN first, then the route
   reflectors), syslog: one ``fabric()`` design pushed in ``staged`` mode —
   the wave engine orders the carriers before their children.

Everything is created as named ``niwaki-*`` policies; the ``default`` fabric
policies are never touched (single documented exception: BGP route reflectors
live under the built-in ``bgpInstPol`` ``default`` — upserted, never created).

Run (the three acts are designed to run in order):
    uv run pytest tests/integration -v -m integration -s
"""

from __future__ import annotations

import time

import pytest

from niwaki import Niwaki
from niwaki.design import Cursor, controller, fabric
from tests.integration.conftest import (
    LEAF1_ID,
    LEAF1_NAME,
    LEAF2_ID,
    LEAF2_NAME,
    SPINE1_ID,
    SPINE1_NAME,
    wipe_all,
)

pytestmark = pytest.mark.integration

# Registration slots: role → [(node_id, name), ...] — consumed in order as
# the discovery cascade surfaces switches.
_SLOTS: dict[str, list[tuple[str, str]]] = {
    "leaf": [(LEAF1_ID, LEAF1_NAME), (LEAF2_ID, LEAF2_NAME)],
    "spine": [(SPINE1_ID, SPINE1_NAME)],
}
# serial → assigned name, shared across this module's ordered tests.
_registered: dict[str, str] = {}


def fabric_design() -> Cursor:
    """Fabric-wide policies in operator vocabulary — one fluent declaration.

    BGP route reflectors live under the built-in ``bgpInstPol`` ``default``
    (upserted, never created) — the single documented exception to the
    "never touch default" rule, because it is the only way.
    """
    return (
        fabric()
        .dns_profile("niwaki-dns", description="niwaki walkthrough DNS")
            .provider("10.0.0.53", prefered_dns_provider=True)
            .domain("niwaki.lab")
        .datetime_policy("niwaki-datetime", description="niwaki walkthrough NTP")
            .ntp_provider("0.pool.ntp.org")
            .ntp_provider("1.pool.ntp.org")
        .syslog_group("niwaki-syslog")
            .remote_destination("10.0.0.99")
        .bgp_instance("default")
            .autonomous_system().set(
                autonomous_system_number=65001,
                description="v0.1.0 ASN lab sim validated"
                )
            .route_reflector().set(description="ASN fabric wide scope")
                .node(LEAF1_ID)
                .node(LEAF2_ID)
    )  # fmt: skip


class Test1FabricBringUp:
    """Day-0: from discovered serial numbers to a configured fabric."""

    # ── 0 · Clean slate for the whole walkthrough ────────────────────────────

    def test_00_start_clean(self, live_aci: Niwaki) -> None:
        wipe_all(live_aci)

    # ── 1 · Discovery cascade: register switches as they appear ─────────────

    def test_01_discovery_cascade_registration(self, live_aci: Niwaki) -> None:
        """Register the fabric the way ACI actually discovers it.

        Only the first leaf talks to the APIC initially; the spine is only
        discovered through a *registered* leaf, and further leaves through
        the spine.  So we poll ``dhcpClient`` (observation — imperative loop)
        and push one small ``controller()`` design per new serial:
        leaf-01/101, leaf-02/102, spine-01/201.
        """
        slots = {role: list(pairs) for role, pairs in _SLOTS.items()}
        deadline = time.monotonic() + 300  # the simulator cascade takes minutes

        while time.monotonic() < deadline and any(slots.values()):
            progress = False
            for client in live_aci.query("dhcpClient").fetch():
                serial = getattr(client, "id", "")
                role = getattr(client, "nodeRole", "")
                if not serial or serial in _registered or not slots.get(role):
                    continue
                node_id, name = slots[role].pop(0)
                controller().fabric_membership().fabric_node_member(
                    serial, id=node_id, name=name, role=role
                ).push(live_aci)
                _registered[serial] = name
                progress = True
                print(f"\nregistered {serial} as {name} (node {node_id})")
            if not any(slots.values()):
                break
            if not progress:
                time.sleep(15)  # let the fabric converge and cascade

        assert _registered, "no switch could be registered from dhcpClient"
        print(f"\nregistered nodes: {_registered} | unfilled slots: {slots}")

    def test_02_membership_reflects_registration(self, live_aci: Niwaki) -> None:
        membership = live_aci.root.controller().fabric_membership_policy()
        idents = {m.name for m in membership.query("fabricNodeIdentP").fetch()}
        assert set(_registered.values()) <= idents

    def test_03_nodes_join_the_fabric(self, live_aci: Niwaki) -> None:
        """Soft convergence check: registered switches become fabricNode."""
        deadline = time.monotonic() + 90
        names: set[str] = set()
        while time.monotonic() < deadline:
            names = {n.name for n in live_aci.query("fabricNode").fetch()}
            if set(_registered.values()) <= names:
                break
            time.sleep(10)
        print(f"\nfabricNode members: {sorted(names)}")
        assert any(name in names for name in _registered.values())

    # ── 3 · Fabric policies: one design, pushed in waves ─────────────────────

    def test_04_fabric_policies_as_one_design(self, live_aci: Niwaki) -> None:
        """NTP + DNS + BGP RR + syslog — one ``fabric()`` design, staged.

        Declaration order is free; the wave engine sorts by DN depth so the
        ``fabricInst`` carrier and the policy parents always land before
        their children.
        """
        report = fabric_design().push(live_aci, mode="staged")

        assert report.dns[0] == "uni/fabric"  # the carrier upsert leads wave 0
        assert report.request_count == 14  # carrier + 4 parents + 9 children

    def test_05_audit_fabric_policies(self, live_aci: Niwaki) -> None:
        """Read back the design like an operator would."""
        fabric_node = live_aci.root.fabric()

        ntp = fabric_node.date_and_time_policy("niwaki-datetime")
        assert ntp.query("datetimeNtpProv").count() == 2

        providers = {
            p.ip_address for p in fabric_node.dns_profile("niwaki-dns").query("dnsProv").fetch()
        }
        assert providers == {"10.0.0.53"}

        assert fabric_node.syslog_monitoring_destination_group("niwaki-syslog").read() is not None

        from niwaki.models.bgp.bgpAsP import bgpAsP

        asys = live_aci.node("uni/fabric/bgpInstP-default/as", bgpAsP).read()
        assert asys.autonomous_system_number == 65001
