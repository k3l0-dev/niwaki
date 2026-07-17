"""Management — IP address pools across combinations and tenants (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/09_management/test_008_address_pools.py -m integration -s

The operator declares the two kinds of tenant address pool, factored across
**several dedicated tenants** so both the combination space and multiple parallel
instances are exercised. Within each tenant:

- **IP address pools** (``fvnsAddrInst``): the cartesian of ``address_type``
  (regular / vip_range) x ``skip_gw_validation`` (both), each with several
  unicast address blocks (``fvnsUcastAddrBlk``).
- The **IP address management pool** (``fvAddrMgmtPool``) with its address
  blocks (``fvAddrMgmtAddrBlk``).

Each tenant also classifies into a user-created security domain through a domain
reference (``aaaDomainRef``). The security domain lives under ``uni/userext``;
the tenants are dedicated ``niwaki-it-*`` tenants. One design, pushed once.
Values are illustrative.

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

SECDOM = "niwaki-it-pool-secdom"
TENANTS = ("niwaki-it-mgmt-pools-0", "niwaki-it-mgmt-pools-1", "niwaki-it-mgmt-pools-2")
# (address_type, skip_gw_validation) — the full fvnsAddrInst cartesian.
IP_COMBOS = (
    ("regular", False),
    ("regular", True),
    ("vip_range", False),
    ("vip_range", True),
)
IPAM_COUNT = 2


def _common(obj: Cursor) -> None:
    """Attach the universal children present on almost every ACI class."""
    obj.mo(tagTag, key="niwaki-it", value="management")
    obj.mo(tagAnnotation, key="niwaki-it-note", value="exhaustive-provisioning")
    obj.mo(aaaRbacAnnotation, domain="all")


def test_address_pools(live_aci: Niwaki) -> None:
    root = design()
    _common(root.aaa().security_domain(SECDOM, description="Security domain for the pool tenants."))

    for ti, tn_name in enumerate(TENANTS):
        ten = root.tenant(tn_name, description="Dedicated tenant for management address pools.")
        ten.mo(aaaDomainRef, name=SECDOM, description="Reference to a user security domain.")
        _common(ten)

        # IP address pools (fvnsAddrInst): address_type x skip_gw_validation.
        for i, (atype, skip) in enumerate(IP_COMBOS):
            base = f"10.{204 + ti}.{i}"
            pool = ten.ip_address_pool(
                f"niwaki-it-ipp-{i}",
                ip_address=f"{base}.1/24",
                address_type=atype,
                skip_gw_validation=skip,
                description=f"IP address pool type {atype}, skip_gw {skip}.",
            )
            pool.ip_address_block(f"{base}.10", f"{base}.50", description="First block.")
            pool.ip_address_block(f"{base}.60", f"{base}.90", description="Second block.")
            pool.ip_address_block(f"{base}.100", f"{base}.150", description="Third block.")
            _common(pool)

        # IP address management pools (fvAddrMgmtPool) with their blocks.
        for i in range(IPAM_COUNT):
            base = f"10.{210 + ti}.{i}"
            ipam = ten.address_pool(
                f"niwaki-it-ipam-{i}", description=f"IP address management pool {i}."
            )
            ipam.block(f"{base}.10", f"{base}.50", description="First management block.")
            ipam.block(f"{base}.60", f"{base}.90", description="Second management block.")
            _common(ipam)

    root.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    dns = [f"uni/tn-{tn_name}" for tn_name in TENANTS]
    dns.append(f"uni/userext/domain-{SECDOM}")
    for dn in dns:
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
