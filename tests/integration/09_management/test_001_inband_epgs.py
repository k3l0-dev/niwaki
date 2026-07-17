"""Management — in-band EPGs over every attribute combination (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/09_management/test_001_inband_epgs.py -m integration -s

The operator lays down a wall of in-band management EPGs (``mgmtInB``) so every
combination of the EPG's classifying attributes is exercised on the controller:
the full cartesian of ``flood_on_encap`` (2) x ``preferred_group_member`` (2) x
``provider_label_match_criteria`` (4), with the QoS class rotated across all
seven priorities. Each EPG gets its own encapsulation VLAN in the 2900-2999 lane
and binds the shared in-band bridge domain.

Everything is **named** (``niwaki-it-*``) under the APIC-managed ``mgmt`` tenant;
the tenant and its ``mgmtp-default`` profile are only *traversed*, never
reconfigured. Values are illustrative — this exercises the SDK surface.

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

# COVERAGE GAPS (already tracked in tests/design/coverage_gaps.json; not forced):
#   fvOrchsInfo / mdpClassId / orchsLDevVipCfg / vnsAbs*/vnsFolderInst/vnsParamInst/
#   vnsCfgRelInst on mgmtInB (MSC-orchestrator + L4-L7 attach families).
# APIC constraints (real, not curation bugs):
#   - contract_master (fvRsSecInherited): only fvAEPg/fvESg/l3extInstP.
#   - static_route/static_node: assign per-node addresses (01_day0/test_006).

TN = "mgmt"

FLOOD = ("disabled", "enabled")
PREF = ("exclude", "include")
MATCH = ("All", "AtleastOne", "AtmostOne", "None")
PRIOS = ("level1", "level2", "level3", "level4", "level5", "level6", "unspecified")

# Deterministic EPG names, one per (flood x pref x match) combination.
EPG_COUNT = len(FLOOD) * len(PREF) * len(MATCH)
EPG_NAMES = [f"niwaki-it-inb-{i:02d}" for i in range(EPG_COUNT)]

VRF = "niwaki-it-inb-epgs-vrf"
BD = "niwaki-it-inb-epgs-bd"


def _common(obj: Cursor) -> None:
    """Attach the universal children present on almost every ACI class."""
    obj.mo(tagTag, key="niwaki-it", value="management")
    obj.mo(tagAnnotation, key="niwaki-it-note", value="exhaustive-provisioning")
    obj.mo(aaaRbacAnnotation, domain="all")


def test_inband_epgs(live_aci: Niwaki) -> None:
    mgmt = tenant(TN)  # traversed, never reconfigured

    vrf = mgmt.vrf(VRF, description="In-band VRF the EPG bridge domain sits on.")
    _common(vrf)
    bd = mgmt.bd(
        BD,
        description="Shared in-band bridge domain for the EPG matrix.",
        unicast_routing=True,
    ).bind(vrf=VRF)
    bd.subnet("10.210.0.1/24", scope="public,shared", description="In-band BD gateway.")
    _common(bd)

    profile = mgmt.management_profile()

    idx = 0
    for flood in FLOOD:
        for pref in PREF:
            for match in MATCH:
                epg = profile.in_band_epg(
                    EPG_NAMES[idx],
                    encap=f"vlan-{2900 + idx}",
                    flood_on_encap=flood,
                    preferred_group_member=pref,
                    provider_label_match_criteria=match,
                    qos_class=PRIOS[idx % len(PRIOS)],
                    description=(
                        f"In-band EPG flag/QoS matrix: flood {flood}, pref {pref}, "
                        f"match {match}, qos {PRIOS[idx % len(PRIOS)]}."
                    ),
                )
                epg.bind(bd=BD)
                _common(epg)
                idx += 1

    mgmt.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    dns = [f"uni/tn-{TN}/mgmtp-default/inb-{name}" for name in EPG_NAMES]
    dns += [f"uni/tn-{TN}/BD-{BD}", f"uni/tn-{TN}/ctx-{VRF}"]
    for dn in dns:
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
