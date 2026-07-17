"""VMM — EPG aggregators, combination coverage (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/07_services/test_012_vmm_aggregators.py -m integration -s

Under VMware VMM domains the operator sweeps the EPG aggregators (``vmmUsrAggr``) across
the cartesian of resolution immediacy x allocation mode x classification preference,
rotating the forged-transmit / MAC-change / promiscuous ``CompConfigMode`` settings and
the untagged-access-port boolean, and varying the feature Flags (none / one / several).
Custom EPG aggregators (``vmmUsrCustomAggr``) get the same treatment. Each aggregator
carries an encap range in the assigned VMM VLAN lane (vlan-2700..2799), sweeping the
block allocation mode and role.

Combination sweep — not a production catalogue. ``wipe(aci)`` (operator-only) deletes
the named domains.

Universal children (``tagInst`` / ``tagExtMngdInst``) carry no typed maker in the DSL.

# COVERAGE GAPS (curated children with no reachable maker/bind — reported, not forced):
#   - bind:vmmRsUsrAggrLagPolAtt@vmmUsrAggr / vmmRsUsrCustomAggrLagPolAtt@vmmUsrCustomAggr
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
DOM = "niwaki-it-aggr-dom"
VLAN_POOL = "niwaki-it-aggr-vlan"

IMMEDIACIES = ("immediate", "lazy")
ALLOC_MODES = ("dynamic", "static")
CLASS_PREFS = ("encap", "useg")
CONFIG_MODES = ("Disabled", "Enabled")
FEATURE_FLAGS = (
    "none",
    "skip-encap-validation",
    "skip-encap-validation,skip-rel-to-eppd",
    "skip-vlan-pool-inheritance",
)
BLOCK_ALLOC = ("dynamic", "inherit", "static")
BLOCK_ROLES = ("external", "internal")


def test_vmm_aggregators(live_aci: Niwaki) -> None:
    dsn = design()
    # A VLAN pool covering the aggregator lane, bound to the domain, so the aggregator
    # trunk-portgroup VLAN ranges resolve inside the domain pool (otherwise the APIC
    # raises "trunk portgroup VLAN ranges are out of domain VLAN pool").
    dsn.infra().vlan_pool(VLAN_POOL, "static").range(
        "vlan-2700", "vlan-2799", allocation_mode="static"
    )
    dom = dsn.vmm_provider(VENDOR).vmm_dom(DOM, encap_mode="vlan")
    dom.bind(vlan_pool=VLAN_POOL)

    forged_cycle = itertools.cycle(CONFIG_MODES)
    mac_cycle = itertools.cycle(CONFIG_MODES)
    promisc_cycle = itertools.cycle(CONFIG_MODES)
    flags_cycle = itertools.cycle(FEATURE_FLAGS)
    untagged_cycle = itertools.cycle((True, False))
    block_alloc_cycle = itertools.cycle(BLOCK_ALLOC)
    block_role_cycle = itertools.cycle(BLOCK_ROLES)
    vlan = 2700

    for index, (imedcy, alloc, class_pref) in enumerate(
        itertools.product(IMMEDIACIES, ALLOC_MODES, CLASS_PREFS)
    ):
        aggregator = dom.epg_aggregator(
            f"epg-aggr-{index:02d}",
            description=f"EPG aggregator {imedcy}/{alloc}/{class_pref}.",
            aggr_imedcy=imedcy,
            alloc_mode=alloc,
            classification_preference=class_pref,
            feature_flags=next(flags_cycle),
            forged_transmit_setting=next(forged_cycle),
            mac_address_changes_setting=next(mac_cycle),
            promiscous_mode_setting=next(promisc_cycle),
            untagged_access_port=next(untagged_cycle),
        )
        aggregator.range(
            f"vlan-{vlan}",
            f"vlan-{vlan}",
            allocation_mode=next(block_alloc_cycle),
            role=next(block_role_cycle),
            description="Aggregator encap block.",
        )
        vlan += 1

    for index, (imedcy, alloc, class_pref) in enumerate(
        itertools.product(IMMEDIACIES, ALLOC_MODES, CLASS_PREFS)
    ):
        custom = dom.custom_epg_aggregator(
            f"custom-aggr-{index:02d}",
            description=f"Custom EPG aggregator {imedcy}/{alloc}/{class_pref}.",
            aggr_imedcy=imedcy,
            alloc_mode=alloc,
            classification_preference=class_pref,
            feature_flags=next(flags_cycle),
            forged_transmit_setting=next(forged_cycle),
            mac_address_changes_setting=next(mac_cycle),
            promiscous_mode_setting=next(promisc_cycle),
            untagged_access_port=next(untagged_cycle),
        )
        custom.range(
            f"vlan-{vlan}",
            f"vlan-{vlan}",
            allocation_mode=next(block_alloc_cycle),
            role=next(block_role_cycle),
            description="Custom aggregator encap block.",
        )
        vlan += 1

    dsn.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (
        f"uni/vmmp-{VENDOR}/dom-{DOM}",
        f"uni/infra/vlanns-[{VLAN_POOL}]-static",
    ):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
