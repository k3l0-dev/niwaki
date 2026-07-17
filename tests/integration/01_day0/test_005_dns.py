"""Day 0 — DNS.

Run:
    uv run pytest tests/integration/01_day0/test_005_dns.py -m integration -s

The operator configures the fabric's DNS on a dedicated profile (``dns-acme``):
providers (one preferred) and search domains (one marked default), reachable
over the OOB management EPG.
"""

from __future__ import annotations

import pytest

from niwaki import Niwaki
from niwaki.design import fabric

pytestmark = pytest.mark.integration

DNS_SERVERS = ("10.0.0.53", "10.0.0.54")
OOB_MGMT_EPG = "uni/tn-mgmt/mgmtp-default/oob-default"  # the fabric's default out-of-band EPG


def test_dns(live_aci: Niwaki) -> None:
    day0 = fabric()

    dns = day0.dns_profile("dns-acme")
    dns.provider(DNS_SERVERS[0], prefered_dns_provider=True)  # the preferred resolver
    dns.provider(DNS_SERVERS[1])
    dns.domain("niwaki.lab", default=True)  # the default search domain
    dns.domain("acme.corp")
    dns.bind_dn(management_epg=OOB_MGMT_EPG)

    day0.push(live_aci)
