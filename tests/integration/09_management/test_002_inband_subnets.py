"""Management — in-band EPG subnets over every fvSubnet combination (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/09_management/test_002_inband_subnets.py -m integration -s

One in-band management EPG carries a wall of subnets so every ``fvSubnet``
combination the SDK can express is exercised: the cartesian of ``scope`` flag
combinations (6) x ``subnet_control`` flag combinations (8), with
``ip_dp_learning`` and ``virtual`` flipped across the set. ``preferred`` is left
False on every one — the APIC allows a preferred subnet only under a bridge
domain, never under an EPG (a real controller constraint, noted below).

Everything is **named** (``niwaki-it-*``) under the APIC-managed ``mgmt`` tenant;
the tenant and its ``mgmtp-default`` profile are only *traversed*. Values are
illustrative.

``wipe(aci)`` (operator-only) removes only the named objects this file creates.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import Cursor, tenant
from niwaki.exceptions import NotFoundError
from niwaki.models.aaa.aaaRbacAnnotation import aaaRbacAnnotation
from niwaki.models.tag.tagAnnotation import tagAnnotation
from niwaki.models.tag.tagTag import tagTag

pytestmark = pytest.mark.integration

# APIC constraints exercised here (real, not curation bugs):
#   - fvSubnet.preferred is only valid under a bridge domain, so every EPG subnet
#     keeps preferred=False (BD-subnet preferred coverage lives in 04_tenant).
#   - fvSubnet anycast/NLB/SCVMM endpoint children are application-EPG features
#     (04_tenant), inappropriate under a management subnet.

TN = "mgmt"
EPG = "niwaki-it-inb-subnets"
ENCAP = "vlan-2950"

# The APIC rejects a subnet scoped both private and public, so those two never
# appear together — every other combination is exercised.
SCOPE_COMBOS = (
    ("private",),
    ("public",),
    ("shared",),
    ("public", "shared"),
    ("private", "shared"),
)
CTRL_COMBOS = (
    ("nd",),
    ("querier",),
    ("no-default-gateway",),
    ("unspecified",),
    ("nd", "querier"),
    ("querier", "no-default-gateway"),
    ("nd", "no-default-gateway"),
    ("nd", "querier", "no-default-gateway"),
)
IP_DP = ("enabled", "disabled")


def _common(obj: Cursor) -> None:
    """Attach the universal children present on almost every ACI class."""
    obj.mo(tagTag, key="niwaki-it", value="management")
    obj.mo(tagAnnotation, key="niwaki-it-note", value="exhaustive-provisioning")
    obj.mo(aaaRbacAnnotation, domain="all")


def test_inband_subnets(live_aci: Niwaki) -> None:
    mgmt = tenant(TN)
    profile = mgmt.management_profile()
    epg = profile.in_band_epg(
        EPG, encap=ENCAP, description="In-band EPG hosting the subnet combination matrix."
    )
    _common(epg)

    idx = 0
    for scope in SCOPE_COMBOS:
        for ctrl in CTRL_COMBOS:
            idx += 1
            epg.subnet(
                f"10.211.{idx}.254/24",
                scope=",".join(scope),
                subnet_control=",".join(ctrl),
                ip_dp_learning=IP_DP[idx % 2],
                virtual=(idx % 2 == 0),
                preferred=False,
                description=f"Subnet scope {'+'.join(scope)}, ctrl {'+'.join(ctrl)}.",
            )

    mgmt.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (f"uni/tn-{TN}/mgmtp-default/inb-{EPG}",):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
