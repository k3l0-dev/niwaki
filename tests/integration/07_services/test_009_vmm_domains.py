"""VMM — domains, combination coverage (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/07_services/test_009_vmm_domains.py -m integration -s

The operator stamps out a spread of VMware VMM domains (``vmmDomP``) sweeping the
access mode, encapsulation mode, switching preference, endpoint-inventory type,
default-encap preference, the ARP-learning and control-knob Flags, and the
port-group / tag-retrieval / VM-folder-retrieval booleans.

Environment note: these domains land ACI-side config; a reachable vCenter is required
for them to sync inventory (they carry a "no controller" fault until then). The AVE-only
knobs (``enable_ave_mode`` / host-availability monitoring, and the ivxlan encap / hw
switching-preference that imply AVE) are out of scope on this simulator.

Combination sweep — not a production catalogue. ``wipe(aci)`` (operator-only) deletes
each named domain (the per-vendor provider container is never deleted).

Universal children (``tagInst`` / ``tagExtMngdInst``) carry no typed maker in the DSL.

# COVERAGE GAPS (curated children with no reachable maker/bind — reported, not forced):
#   - maker:vmmIntAggr@vmmDomP / aaaDomainRef@vmmDomP / vmmOrchsProv@vmmDomP (NDO)
#   - bind:vmmRsPrefEnhancedLagPol@vmmDomP
"""

from __future__ import annotations

import contextlib
import itertools

import pytest

from niwaki import Niwaki
from niwaki.design import design
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

VENDOR = "VMware"

# Every DVS-valid value of each axis, swept as a full cartesian below. (AVS/AVE-only
# values — ivxlan / unknown encap, hw / unknown switching preference, arp-learning
# enabled — are excluded; see the environment note in the docstring.)
ACCESS_MODES = ("read-only", "read-write")
ENCAP_MODES = ("vlan", "vxlan")
EP_INVENTORY = ("none", "on-link")
DEFAULT_ENCAPS = ("unspecified", "vlan", "vxlan")
DOMAIN_COUNT = len(ACCESS_MODES) * len(ENCAP_MODES) * len(EP_INVENTORY) * len(DEFAULT_ENCAPS)


def test_vmm_domains(live_aci: Niwaki) -> None:
    dsn = design()
    provider = dsn.vmm_provider(VENDOR)

    ctrl_knob_cycle = itertools.cycle(("none", "epDpVerify"))
    cfg_pg_cycle = itertools.cycle((True, False))
    tag_cycle = itertools.cycle((True, False))
    folder_cycle = itertools.cycle((True, False))

    for index, (access_mode, encap_mode, ep_inventory, default_encap) in enumerate(
        itertools.product(ACCESS_MODES, ENCAP_MODES, EP_INVENTORY, DEFAULT_ENCAPS)
    ):
        # A vxlan-encap domain needs a multicast group address.
        mcast = f"224.1.{index}.1" if encap_mode == "vxlan" else None
        provider.vmm_dom(
            f"niwaki-it-d{index:02d}",
            access_mode=access_mode,
            arp_learning="disabled",
            # ENV FAULT: both configure_infra_port_group values are swept (child-closure
            # rule). The APIC accepts the config, but a domain with it enabled raises a
            # deployment-layer F2247 ("Infrastructure VLAN needs to be configured for
            # Infra-PG. Please enable under AEP") until the fabric-wide infra-VLAN-on-AEP
            # scaffolding exists — that scaffolding is fabric-access / day-0 setup,
            # outside this isolated services sweep. See the 07 README.
            configure_infra_port_group=next(cfg_pg_cycle),
            ctrl_knob=next(ctrl_knob_cycle),
            enable_tag_data_retrieval=next(tag_cycle),
            enable_vm_folder_data_retrieval=next(folder_cycle),
            encap_mode=encap_mode,
            switching_preference="sw",
            ep_inventory_type=ep_inventory,
            end_point_retention_time_seconds=15,
            multicast_address=mcast,
            virtual_switch="default",
            default_encap_mode=default_encap,
        )

    dsn.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for index in range(DOMAIN_COUNT):
        dn = f"uni/vmmp-{VENDOR}/dom-niwaki-it-d{index:02d}"
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
