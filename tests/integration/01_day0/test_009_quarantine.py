"""Day 0 — the 802.1x fail-auth quarantine tenant.

Run:
    uv run pytest tests/integration/01_day0/test_009_quarantine.py -m integration -s

A dedicated ``no-mans-land`` tenant holds the quarantine EPG where endpoints that
fail 802.1x authentication land — a shared security zone, deliberately kept out
of any user tenant. The fabric's 802.1x node auth policy points its fail-auth EPG
here.
"""

from __future__ import annotations

import pytest

from niwaki import Niwaki
from niwaki.design import tenant

pytestmark = pytest.mark.integration

TENANT = "no-mans-land"


def test_quarantine_tenant(live_aci: Niwaki) -> None:
    tn = tenant(TENANT)
    tn.vrf("quarantine")
    tn.bd("quarantine").bind(vrf="quarantine")
    tn.app("access").epg("quarantine").bind(bd="quarantine")

    tn.push(live_aci)
