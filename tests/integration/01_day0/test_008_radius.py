"""Day 0 — RADIUS for 802.1x node authentication.

Run:
    uv run pytest tests/integration/01_day0/test_008_radius.py -m integration -s

The operator sets up the RADIUS servers and a provider group under
``uni/userext/radiusext`` — the group the fabric's 802.1x node authentication
policy points at (referenced from the access policies).
"""

from __future__ import annotations

import pytest

from niwaki import Niwaki
from niwaki.design import aaa

pytestmark = pytest.mark.integration

RADIUS_SERVERS = ("10.0.0.40", "10.0.0.41")
RADIUS_KEY = "niwaki-radius-secret"
RADIUS_GROUP = "dot1x-radius"


def test_radius(live_aci: Niwaki) -> None:
    cfg = aaa()
    radius = cfg.radius()

    # The RADIUS servers (name = the server address).
    for server in RADIUS_SERVERS:
        radius.radius_provider(
            server,
            description=f"RADIUS server {server} for 802.1x.",
            key=RADIUS_KEY,
            authentication_protocol="pap",
            port=1812,
            timeout_in_seconds=5,
            retries=2,
        )

    # The provider group, referencing the servers in try-order.
    group = radius.radius_provider_group(
        RADIUS_GROUP,
        description="RADIUS provider group for 802.1x node authentication.",
    )
    for order, server in enumerate(RADIUS_SERVERS, start=1):
        group.provider(server, order_in_which_providers_are_tried=order)

    cfg.push(live_aci)
