"""Tenant — Fibre-Channel and legacy bridge domains (non-prod).

Run:
    uv run pytest tests/integration/04_tenant/test_008_bd_variants.py -m integration -s

The BD variants that carry APIC-specific rules: a Fibre-Channel BD (which must
have unicast routing disabled) and a legacy-mode BD (which pins the access
encapsulation).

Values are illustrative. This file owns tenant ``niwaki-it-bd-var``; ``wipe``
(operator-only) deletes it.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

TN = "niwaki-it-bd-var"
VRF = "niwaki-it-bd-var-vrf"


def test_bd_variants(live_aci: Niwaki) -> None:
    """The Fibre-Channel and legacy BD variants."""
    tn = tenant(TN, description="Fibre-Channel and legacy BD variants")
    tn.vrf(VRF, description="VRF backing the BD variants.")

    # An FC BD must have unicast routing disabled.
    tn.bd(
        "niwaki-it-bd-fc",
        description="Fibre-Channel bridge domain.",
        type="fc",
        unicast_routing=False,
    ).bind(vrf=VRF)

    # A legacy-mode BD pins the access encapsulation.
    tn.bd(
        "niwaki-it-bd-legacy",
        description="Legacy-mode bridge domain.",
    ).bind(vrf=VRF).legacy_mode(
        description="Legacy-mode child pinning the BD access encapsulation.",
        bd_access_encap="vlan-2599",
    )

    tn.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (f"uni/tn-{TN}",):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
