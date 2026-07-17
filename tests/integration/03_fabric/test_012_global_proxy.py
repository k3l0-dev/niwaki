"""Fabric — global endpoint controls and proxy (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/03_fabric/test_012_global_proxy.py -m integration -s

The fabric-wide global settings: the global endpoint-listen policy (a singleton,
enabled on a VLAN encapsulation — the APIC requires a valid VLAN id), the
management connectivity preference (a singleton), and the HTTP/HTTPS proxy server
(a fixed-RN object carrying several ignore-host entries).

Illustrative values — not a real fabric config.

``wipe(aci)`` (operator-only) removes the proxy server; the two singletons it
re-configures in place are left as-is.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import fabric
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

# VLAN lane reserved for these walkthroughs.
GLOBAL_EP_ENCAP = "vlan-3000"
IGNORE_HOSTS = ("10.0.0.0/8", "172.16.0.0/12", "192.0.2.0/24")


def test_global_endpoint_listen(live_aci: Niwaki) -> None:
    # A fabric singleton — both enabled states are factored across two pushes.
    # When enabled, the encapsulation must be a VLAN with a valid VLAN id.
    fabric().global_ep_listen_policy(
        description="Global endpoint-listen policy enabled on a VLAN encapsulation.",
        enabled=True,
        encap=GLOBAL_EP_ENCAP,
    ).push(live_aci)
    fabric().global_ep_listen_policy(
        description="Global endpoint-listen policy disabled.",
        enabled=False,
    ).push(live_aci)


def test_connectivity_preference(live_aci: Niwaki) -> None:
    # A fabric singleton — both interface preferences are factored across pushes.
    for pref in ("inband", "ooband"):
        fabric().mgmt_connectivity_preference(
            description=f"Prefer {pref} for APIC-originated connectivity.",
            interface_pref=pref,
        ).push(live_aci)


def test_proxy_server(live_aci: Niwaki) -> None:
    fab = fabric()
    proxy = fab.proxy_server(
        http_url="http://proxy.niwaki.example:8080",
        https_url="https://proxy.niwaki.example:8443",
    )
    for host in IGNORE_HOSTS:
        proxy.ignore_host(host)
    fab.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it.

    The global endpoint-listen and connectivity-preference singletons are left
    as-is; only the proxy server is removed.
    """
    with contextlib.suppress(NotFoundError):
        aci.node("uni/fabric/server").delete()
