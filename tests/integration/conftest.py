"""
Integration test configuration for niwaki.

Integration tests run against a live APIC.  Credentials come exclusively
from environment variables (loaded from ``.env`` at the repo root by
tests/conftest.py) — there are no hardcoded defaults.  When a variable is
missing, the integration suite is skipped instead of failing.

Environment variables:
    APIC_HOST     : Target APIC URL (e.g. https://192.0.2.10).
    APIC_USERNAME : APIC username.
    APIC_PASSWORD : APIC password.
"""

from __future__ import annotations

import os
from collections.abc import Generator

import pytest
import stamina

from niwaki import Niwaki
from niwaki.exceptions import TransportError
from niwaki.transport.session import ApicSession

# Keep stamina in testing mode for integration tests too — real retries
# without sleep delays are fine here.


@pytest.fixture(autouse=True, scope="session")
def _stamina_testing() -> None:
    """Keep stamina in testing mode to avoid waiting between retries."""
    stamina.set_testing(True)


# ── Credential fixtures (env-only) ────────────────────────────────────────────


def _require_env(name: str) -> str:
    """Return the env var value or skip the integration suite when absent."""
    value = os.getenv(name)
    if not value:
        pytest.skip(f"{name} not set — integration tests require a live APIC (see .env)")
    return value


@pytest.fixture(scope="session")
def apic_host() -> str:
    """Base URL of the target APIC (env-only, no default)."""
    return _require_env("APIC_HOST")


@pytest.fixture(scope="session")
def apic_username() -> str:
    """APIC username (env-only, no default)."""
    return _require_env("APIC_USERNAME")


@pytest.fixture(scope="session")
def apic_password() -> str:
    """APIC password (env-only, no default)."""
    return _require_env("APIC_PASSWORD")


@pytest.fixture(scope="session")
def live_session(
    apic_host: str, apic_username: str, apic_password: str
) -> Generator[ApicSession, None, None]:
    """
    Authenticated APIC session against the live lab APIC.

    Session-scoped: a single login for the entire integration suite.
    The session is closed automatically at the end of the pytest session.

    Yields:
        Authenticated ``ApicSession`` ready for use.
    """
    session = ApicSession(
        host=apic_host,
        username=apic_username,
        password=apic_password,
        verify_ssl=False,  # lab simulators use self-signed certificates
    )
    try:
        session.login()
    except TransportError:
        pytest.skip(f"APIC {apic_host} unreachable — integration suite skipped")
    yield session  # type: ignore[misc]
    session.close()


@pytest.fixture(scope="session")
def live_aci(
    apic_host: str, apic_username: str, apic_password: str
) -> Generator[Niwaki, None, None]:
    """Authenticated :class:`~niwaki.Niwaki` facade against the live lab APIC.

    Session-scoped: a single login shared across the whole integration suite.
    Closed automatically at the end of the pytest session.

    Yields:
        Authenticated :class:`~niwaki.Niwaki` instance ready for use.
    """
    try:
        aci = Niwaki.connect(
            apic_host,
            apic_username,
            apic_password,
            verify_ssl=False,
        )
    except TransportError:
        pytest.skip(f"APIC {apic_host} unreachable — integration suite skipped")
    yield aci  # type: ignore[misc]
    aci.close()


# ── Walkthrough topology (shared by the three acts) ───────────────────────────
# The three integration files tell one story on the APIC simulator:
#   test_01_fabric  — bring-up: node registration + fabric-wide policies
#   test_02_access  — cabling: interface policies → profiles → switch profiles
#   test_03_tenant  — application: the three-tier app published on that cabling
# They are designed to run in order (pytest collects them alphabetically).

LEAF1_ID, LEAF2_ID, SPINE1_ID = "101", "102", "201"
LEAF1_NAME, LEAF2_NAME, SPINE1_NAME = "leaf-01", "leaf-02", "spine-01"

# Convention: the leaf interface profile carries the SAME name as its leaf
# switch profile; inside it, one port selector per interface named 1.01-1.58
# (eth1/59-60 are reserved and never get a selector).
IFPROF_LEAF1, IFPROF_LEAF2 = LEAF1_NAME, LEAF2_NAME
VPC_PAIR_NAME = "niwaki-101-102"
VPC_PG = "niwaki-vpc-esxi"
PC_PG = "niwaki-pc-uplink"
ACCESS_PG = "niwaki-access-host"

VLAN_POOL = "niwaki-static"
PHYS_DOM = "niwaki-phys"
L3_DOM = "niwaki-l3"
AAEP = "niwaki-aaep"
VLAN_POOL_DN = f"uni/infra/vlanns-[{VLAN_POOL}]-static"
PHYS_DOM_DN = f"uni/phys-{PHYS_DOM}"
AAEP_DN = f"uni/infra/attentp-{AAEP}"

TENANT = "niwaki-showcase"
TENANT_DEV = "niwaki-showcase-dev"
VPC_PATH_DN = f"topology/pod-1/protpaths-{LEAF1_ID}-{LEAF2_ID}/pathep-[{VPC_PG}]"

IF_POLICIES = {  # name → facade jargon maker on infra (used by the wipes)
    "niwaki-cdp-on": "cdp_policy",
    "niwaki-lldp-on": "lldp_policy",
    "niwaki-lacp-active": "lacp_policy",
    "niwaki-10g": "link_level_policy",
    "niwaki-mcp-on": "mcp_policy",
    "niwaki-bpdu-guard": "stp_policy",
    "niwaki-storm": "storm_control_interface_policy",
}


def wipe_tenant(aci: Niwaki) -> None:
    """Remove act-3 objects (tenants)."""
    import contextlib

    for name in (TENANT, TENANT_DEV):
        with contextlib.suppress(Exception):
            aci.tenant(name).delete()


def wipe_access(aci: Niwaki) -> None:
    """Remove act-2 objects, children before their referenced targets."""
    import contextlib

    infra = aci.root.infra()
    for node in (
        aci.root.fabric()
        .virtual_port_channel_security_policy()
        .vpc_explicit_protection_group(VPC_PAIR_NAME),
        infra.leaf_profile(LEAF1_NAME),
        infra.leaf_profile(LEAF2_NAME),
        infra.spine_profile(SPINE1_NAME),
        infra.access_port_profile(IFPROF_LEAF1),
        infra.access_port_profile(IFPROF_LEAF2),
        infra.func_profile().port_channel(VPC_PG),
        infra.func_profile().port_channel(PC_PG),
        infra.func_profile().leaf_access_port_policy_group(ACCESS_PG),
        infra.aaep(AAEP),
        aci.root.phys_dom(PHYS_DOM),
        aci.root.l3_dom(L3_DOM),
        infra.vlan_pool(name=VLAN_POOL, allocation_mode="static"),
    ):
        with contextlib.suppress(Exception):
            node.delete()
    for name, maker in IF_POLICIES.items():
        with contextlib.suppress(Exception):
            getattr(infra, maker)(name).delete()


def wipe_fabric(aci: Niwaki) -> None:
    """Remove act-1 objects (named fabric policies; node registration stays —
    unregistering simulator nodes mid-suite would break the following acts)."""
    import contextlib

    fabric = aci.root.fabric()
    for node in (
        fabric.date_and_time_policy("niwaki-datetime"),
        fabric.dns_profile("niwaki-dns"),
        # only the RR node we added under the built-in default policy
        fabric.bgp_route_reflector_policy("default")
        .bgp_route_reflector()
        .route_reflector_node_policy_ep(node_id=LEAF1_ID),
        fabric.syslog_monitoring_destination_group("niwaki-syslog"),
    ):
        with contextlib.suppress(Exception):
            node.delete()


def wipe_all(aci: Niwaki) -> None:
    """Full walkthrough cleanup, in reverse dependency order."""
    wipe_tenant(aci)
    wipe_access(aci)
    wipe_fabric(aci)
