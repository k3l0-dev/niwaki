"""Fabric — DNS profiles and the geo location tree (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/03_fabric/test_010_dns_geo.py -m integration -s

One DNS profile per IP-version preference (IPv4 / IPv6), each carrying preferred
and non-preferred providers and a default and non-default search domain, and
associated with the out-of-band management EPG. The geo location tree exercises
the full site → building → floor → room → (row →) rack nesting, with racks
hanging both directly off a room and off a row.

Exhaustive combination coverage, illustrative values — not a real fabric config.

``wipe(aci)`` (operator-only) removes the DNS profiles and geo sites.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import fabric
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

OOB_MGMT_EPG = "uni/tn-mgmt/mgmtp-default/oob-default"

DNS_IPV4 = "niwaki-it-dns-ipv4"
DNS_IPV6 = "niwaki-it-dns-ipv6"
GEO_A = "niwaki-it-geo-a"
GEO_B = "niwaki-it-geo-b"

# (name, ip preference, preferred provider, secondary provider)
DNS_PROFILES = (
    (DNS_IPV4, "IPv4", "8.8.8.8", "8.8.4.4"),
    (DNS_IPV6, "IPv6", "2001:4860:4860::8888", "2001:4860:4860::8844"),
)


def test_dns_profiles(live_aci: Niwaki) -> None:
    fab = fabric()
    for name, ip_pref, primary, secondary in DNS_PROFILES:
        profile = fab.dns_profile(
            name,
            description=f"DNS profile with a {ip_pref} preference.",
            ip_protocol_version=ip_pref,
        )
        profile.provider(primary, prefered_dns_provider=True)
        profile.provider(secondary, prefered_dns_provider=False)
        profile.domain(
            "niwaki.example",
            description="Default search domain.",
            default=True,
        )
        profile.domain(
            "lab.niwaki.example",
            description="Secondary search domain.",
            default=False,
        )
        profile.bind_dn(management_epg=OOB_MGMT_EPG)
    fab.push(live_aci)


def test_geo_location_tree(live_aci: Niwaki) -> None:
    fab = fabric()
    for site_name in (GEO_A, GEO_B):
        site = fab.geo_site(site_name, description=f"Geo site {site_name}.")
        for b in range(2):
            building = site.geo_building(f"building-{b}", description=f"Building {b}.")
            for f in range(2):
                floor = building.geo_floor(f"floor-{f}", description=f"Floor {f}.")
                room = floor.geo_room(f"room-{f}", description=f"Room {f}.")
                # A rack directly under the room, plus racks under a row.
                room.geo_rack("rack-direct", description="Rack directly under the room.")
                row = room.geo_row("row-0", description="Row 0.")
                row.geo_rack("rack-0", description="Rack 0 in row 0.")
                row.geo_rack("rack-1", description="Rack 1 in row 0.")
    fab.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (
        f"uni/fabric/dnsp-{DNS_IPV4}",
        f"uni/fabric/dnsp-{DNS_IPV6}",
        f"uni/fabric/site-{GEO_A}",
        f"uni/fabric/site-{GEO_B}",
    ):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
