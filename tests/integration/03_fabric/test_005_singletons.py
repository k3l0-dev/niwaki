"""Fabric — protocol singletons and power-supply redundancy (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/03_fabric/test_005_singletons.py -m integration -s

The fabric protocol singletons — COOP group, load-balance, WWN and the BGP
route-reflector instance — the APIC ships exactly one of each, so the maker
configures that instance in place (the B1 pattern). Because a singleton holds one
state at a time, mutually-exclusive settings are **factored** across successive
pushes: every valid COOP authentication mode and every valid load-balance
combination is pushed in turn (each proven to be accepted). Alongside them the
power-supply redundancy policy, which is named and creatable, is provisioned once
per admin redundancy mode.

Illustrative values — not a real fabric config.

``wipe(aci)`` (operator-only) removes the named PSU policies; the singletons it
re-configures in place cannot be deleted and are left as-is.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import fabric
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

PSU = "niwaki-it-psu"
# Every redundancy mode the APIC schema offers is accepted by the controller.
PSU_MODES = (
    "comb",
    "ps-rdn",
    "n-rdn",
    "rdn",
    "insrc-rdn",
    "sinin-rdn",
    "not-supp",
    "unknown",
)
# (load-balancing mode, DLB mode, prioritization, GTP hashing) — every valid
# combination the APIC accepts (flowlet prioritization requires traditional LB
# with DLB off; the three are otherwise mutually exclusive).
LB_COMBINATIONS = (
    ("traditional", "off", "on", True),
    ("lfr", "off", "off", True),
    ("traditional", "aggressive", "off", False),
    ("traditional", "conservative", "off", False),
)


def test_coop_authentication_modes(live_aci: Niwaki) -> None:
    # The COOP singleton holds one mode at a time — both are pushed in turn.
    for auth in ("strict", "compatible"):
        fabric().coop_group_policy(
            description=f"Fabric COOP group, {auth} session type.",
            authentication_type=auth,
        ).push(live_aci)


def test_load_balance_modes(live_aci: Niwaki) -> None:
    # The load-balance singleton holds one combination at a time — every valid
    # (LB mode, DLB mode, prioritization) combination is pushed in turn.
    for lb_mode, dlb_mode, prioritization, gtp in LB_COMBINATIONS:
        fabric().load_balance_policy(
            description=f"Load balancing {lb_mode}, DLB {dlb_mode}, priority {prioritization}.",
            load_balancing_mode=lb_mode,
            dynamic_load_balancing_mode=dlb_mode,
            prioritization_mode=prioritization,
            hash_gtp_policy=gtp,
        ).push(live_aci)


def test_wwn_singleton(live_aci: Niwaki) -> None:
    fab = fabric()
    fab.wwn_inst_policy(description="Fabric WWN instance policy (default instance).")
    fab.push(live_aci)


def test_bgp_route_reflector_singleton(live_aci: Niwaki) -> None:
    fab = fabric()
    bgp = fab.bgp_instance(
        "default",
        description="Fabric BGP route-reflector policy and autonomous system.",
    )
    bgp.autonomous_system(
        description="Fabric-wide BGP autonomous system number.",
        autonomous_system_number=65001,
    )
    reflector = bgp.route_reflector(description="Intra-fabric BGP route reflector.")
    for node in live_aci.query("fabricNode").fetch():
        data = node.model_dump(by_alias=True)
        if data.get("role") != "spine" or not data.get("id"):
            continue
        reflector.node(
            int(data["id"]),
            pod_id=_pod_of(str(data.get("dn", ""))),
            description=f"Route-reflector node endpoint for spine {data['id']}.",
        )
    fab.push(live_aci)


def test_power_supply_redundancy(live_aci: Niwaki) -> None:
    fab = fabric()
    for mode in PSU_MODES:
        fab.power_supply_redundancy_policy(
            f"{PSU}-{mode}",
            description=f"Power-supply redundancy in {mode} mode.",
            admin_redundancy_mode=mode,
        )
    fab.push(live_aci)


def _pod_of(dn: str) -> int:
    """Extract the pod id from a ``topology/pod-N/node-M`` DN (defaults to 1)."""
    for part in dn.split("/"):
        if part.startswith("pod-"):
            with contextlib.suppress(ValueError):
                return int(part.removeprefix("pod-"))
    return 1


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it.

    The singletons (COOP, load-balance, WWN, BGP instance) cannot be deleted and
    are left as-is; only the named PSU policies are removed.
    """
    for mode in PSU_MODES:
        with contextlib.suppress(NotFoundError):
            aci.node(f"uni/fabric/psuInstP-{PSU}-{mode}").delete()
