"""Day 0 — NTP.

Run:
    uv run pytest tests/integration/01_day0/test_002_ntp.py -m integration -s

The operator points the fabric at its time sources with authentication enabled:
the public pool (0.pool preferred, 1.pool backup) and the authenticated
corporate server, all reachable over the OOB management EPG.
"""

from __future__ import annotations

import pytest

from niwaki import Niwaki
from niwaki.design import fabric

pytestmark = pytest.mark.integration

NTP_SERVERS = ("0.pool.ntp.org", "1.pool.ntp.org")
CORPORATE_NTP = "acme-corporate"
NTP_AUTH_KEY_ID = 1
NTP_AUTH_KEY = "niwaki-ntp-key"
OOB_MGMT_EPG = "uni/tn-mgmt/mgmtp-default/oob-default"  # the fabric's default out-of-band EPG


def test_ntp(live_aci: Niwaki) -> None:
    day0 = fabric()
    ntp = day0.datetime_policy("ntp")
    ntp.set(admin_state="enabled", server_mode="enabled", authentication_state="enabled")

    # An NTP authentication key, trusted for the corporate server.
    ntp.ntp_auth_key(
        NTP_AUTH_KEY_ID, key=NTP_AUTH_KEY, type_of_authentication_key="md5", trusted_state=True
    )

    # Providers — each reachable over the OOB management EPG.
    ntp.ntp_provider(NTP_SERVERS[0], preferred_state=True).bind_dn(management_epg=OOB_MGMT_EPG)
    ntp.ntp_provider(NTP_SERVERS[1]).bind_dn(management_epg=OOB_MGMT_EPG)

    # The corporate server authenticates — it trusts NTP key id 1 (NTP_AUTH_KEY).
    acme = ntp.ntp_provider(CORPORATE_NTP)
    acme.authentication_key(NTP_AUTH_KEY_ID)
    acme.bind_dn(management_epg=OOB_MGMT_EPG)

    day0.push(live_aci)
