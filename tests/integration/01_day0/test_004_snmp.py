"""Day 0 — SNMP.

Run:
    uv run pytest tests/integration/01_day0/test_004_snmp.py -m integration -s

The operator configures SNMP on the fabric, combining every credential model in
one policy: a v2c community, two SNMPv3 users (one authPriv, one authNoPriv),
the NMS client group associated to the OOB management EPG, trap-forward servers,
and two external collectors — the same fabric traps sent both v2c and v3.
"""

from __future__ import annotations

import pytest

from niwaki import Niwaki
from niwaki.design import fabric

pytestmark = pytest.mark.integration

SNMP_COMMUNITY = "niwaki"  # v2c
SNMP_MANAGER = "10.0.0.50"
SNMP_TRAP_SERVERS = ("10.0.0.60", "10.0.0.61")
OOB_MGMT_EPG = "uni/tn-mgmt/mgmtp-default/oob-default"  # the fabric's default out-of-band EPG

# SNMPv3 users (lab secrets).
V3_USER_AUTHPRIV = "netops"
V3_USER_AUTH = "monitor"
AUTH_KEY = "niwaki-auth-key"
PRIV_KEY = "niwaki-priv-key"


def test_snmp(live_aci: Niwaki) -> None:
    day0 = fabric()

    snmp = day0.snmp_policy("snmp")
    snmp.set(admin_state="enabled", contact="netops@niwaki.lab", location="niwaki lab")

    # v2c — a community string.
    snmp.snmp_community(SNMP_COMMUNITY)

    # v3 — two users: one authPriv (SHA-256 + AES), one authNoPriv (SHA-1).
    snmp.user_profile(
        V3_USER_AUTHPRIV,
        authentication_type="hmac-sha2-256",
        authentication_key=AUTH_KEY,
        privacy="aes-128",
        privacy_key=PRIV_KEY,
    )
    snmp.user_profile(
        V3_USER_AUTH,
        authentication_type="hmac-sha1-96",
        authentication_key=AUTH_KEY,
    )

    # Access — the NMS client group, associated to the OOB management EPG.
    nms = snmp.snmp_client_group_profile("nms")
    nms.bind_dn(management_epg=OOB_MGMT_EPG)
    nms.client_entry(SNMP_MANAGER, name="nms-server")

    for trap_server in SNMP_TRAP_SERVERS:
        snmp.trap_forward_server(trap_server)

    # External collectors — the same fabric traps sent two ways: v2c and v3.
    collector = day0.snmp_monitoring_destination_group("external-collector")
    collector.snmp_trap_destination(
        host="10.0.0.70", port=162, version="v2c", security_name=SNMP_COMMUNITY
    ).bind_dn(management_epg=OOB_MGMT_EPG)
    collector.snmp_trap_destination(
        host="10.0.0.71",
        port=162,
        version="v3",
        security_name=V3_USER_AUTHPRIV,
        v3_security_level="priv",
    ).bind_dn(management_epg=OOB_MGMT_EPG)

    day0.push(live_aci)
