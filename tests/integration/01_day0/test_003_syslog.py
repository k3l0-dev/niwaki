"""Day 0 — syslog.

Run:
    uv run pytest tests/integration/01_day0/test_003_syslog.py -m integration -s

The operator points the fabric at its syslog collectors: the legacy server,
disabled and kept for reference, and the current TLS collector reached over the
OOB management EPG.
"""

from __future__ import annotations

import pytest

from niwaki import Niwaki
from niwaki.design import fabric

pytestmark = pytest.mark.integration

SYSLOG_SERVER = "10.0.0.99"  # legacy collector, being retired
SYSLOG_SERVER_TLS = "10.0.0.98"  # current TLS syslog collector
OOB_MGMT_EPG = "uni/tn-mgmt/mgmtp-default/oob-default"  # the fabric's default out-of-band EPG


def test_syslog(live_aci: Niwaki) -> None:
    day0 = fabric()
    syslog = day0.syslog_group("syslog")
    syslog.remote_destination(
        SYSLOG_SERVER, name="depreciate-syslog-server", admin_state="disabled"
    )
    syslog.remote_destination(SYSLOG_SERVER_TLS, name="syslog-tls", protocol="ssl").bind_dn(
        management_epg=OOB_MGMT_EPG
    )
    day0.push(live_aci)
