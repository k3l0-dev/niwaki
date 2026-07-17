"""Management — endpoint tags across VRFs, BDs and tenants (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/09_management/test_007_endpoint_tags.py -m integration -s

Endpoint tags label endpoints so an ESG tag selector can match them. This file
factors the coverage across **several dedicated tenants** — each with its own
``fvEpTags`` container — and, within each, spreads the tags over its VRFs and
bridge domains: **MAC** tags (``fvEpMacTag``, keyed by MAC + bridge domain,
classifying into a VRF) and **IP** tags (``fvEpIpTag``, keyed by IP + VRF), every
tag with a distinct numeric id. Each tenant also classifies into a user-created
security domain through a domain reference (``aaaDomainRef``).

The security domain lives under ``uni/userext``; the tenants are dedicated
``niwaki-it-*`` tenants. One closed-world design, pushed once. Values are
illustrative.

``wipe(aci)`` (operator-only) deletes the dedicated tenants and the security
domain, and everything under them.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import Cursor, design
from niwaki.exceptions import NotFoundError
from niwaki.models.aaa.aaaDomainRef import aaaDomainRef
from niwaki.models.aaa.aaaRbacAnnotation import aaaRbacAnnotation
from niwaki.models.tag.tagAnnotation import tagAnnotation
from niwaki.models.tag.tagTag import tagTag

pytestmark = pytest.mark.integration

SECDOM = "niwaki-it-eptag-secdom"
TENANTS = ("niwaki-it-mgmt-eptags-0", "niwaki-it-mgmt-eptags-1", "niwaki-it-mgmt-eptags-2")
VRFS = ("niwaki-it-vrf-0", "niwaki-it-vrf-1")
BDS = ("niwaki-it-bd-0", "niwaki-it-bd-1")
PER_GROUP = 5  # tags per BD (MAC) and per VRF (IP), within each tenant


def _common(obj: Cursor) -> None:
    """Attach the universal children present on almost every ACI class."""
    obj.mo(tagTag, key="niwaki-it", value="management")
    obj.mo(tagAnnotation, key="niwaki-it-note", value="exhaustive-provisioning")
    obj.mo(aaaRbacAnnotation, domain="all")


def test_endpoint_tags(live_aci: Niwaki) -> None:
    root = design()
    _common(root.aaa().security_domain(SECDOM, description="Security domain for the tag tenants."))

    for ti, tn_name in enumerate(TENANTS):
        ten = root.tenant(tn_name, description="Dedicated tenant for management endpoint tags.")
        ten.mo(aaaDomainRef, name=SECDOM, description="Reference to a user security domain.")
        _common(ten)

        for vi, vrf in enumerate(VRFS):
            _common(ten.vrf(vrf, description=f"VRF {vi} the endpoint tags classify into."))
        for bi, bd in enumerate(BDS):
            _common(ten.bd(bd, description=f"BD {bi} the MAC tags key on.").bind(vrf=VRFS[bi]))

        et = ten.endpoint_tags()
        _common(et)

        # MAC endpoint tags — spread across every BD, distinct ids.
        mac = 0
        for bi, bd in enumerate(BDS):
            for j in range(PER_GROUP):
                mac += 1
                et.mac_endpoint(
                    f"00:11:{ti:02x}:{bi:02x}:{j:02x}:01",
                    bd,
                    vrf_name=VRFS[bi],
                    id=1000 + ti * 100 + mac,
                    name=f"mac-{ti}-{bi}-{j}",
                )

        # IP endpoint tags — spread across every VRF, distinct ids.
        ip = 0
        for vi, vrf in enumerate(VRFS):
            for j in range(PER_GROUP):
                ip += 1
                et.ip_endpoint(
                    f"10.{203 + ti}.{vi}.{10 + j}",
                    vrf,
                    id=2000 + ti * 100 + ip,
                    name=f"ip-{ti}-{vi}-{j}",
                )

    root.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    dns = [f"uni/tn-{tn_name}" for tn_name in TENANTS]
    dns.append(f"uni/userext/domain-{SECDOM}")
    for dn in dns:
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
