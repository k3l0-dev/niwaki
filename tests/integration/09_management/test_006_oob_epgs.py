"""Management — out-of-band and external management EPGs (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/09_management/test_006_oob_epgs.py -m integration -s

The out-of-band world has two endpoint-group classes, each keyed only by a QoS
priority. This file builds one of each per priority (all seven values):

- **Out-of-band EPGs** (``mgmtOoB``) that *provide* the out-of-band contract.
- **External management networks** (``mgmtInstP``, under the singleton
  ``extmgmt-default`` entity), each with imported subnets, that *consume* it.

Everything is **named** (``niwaki-it-*``) under the APIC-managed ``mgmt`` tenant;
the tenant, its ``mgmtp-default`` profile and its ``extmgmt-default`` entity are
only *traversed*. Values are illustrative.

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

# COVERAGE GAPS / deliberate scoping:
#   - static_node (mgmtRsOoBStNode) / static_route: assign per-node OOB addresses
#     and rewrite the sim's reachability — exercised only in 01_day0/test_006.

TN = "mgmt"
PRIOS = ("level1", "level2", "level3", "level4", "level5", "level6", "unspecified")
CONTRACT = "niwaki-it-oobepg-ctr"
FILTER = "niwaki-it-oobepg-flt"

OOB_NAMES = [f"niwaki-it-oob-{p}" for p in PRIOS]
EXT_NAMES = [f"niwaki-it-ext-{p}" for p in PRIOS]


def _common(obj: Cursor) -> None:
    """Attach the universal children present on almost every ACI class."""
    obj.mo(tagTag, key="niwaki-it", value="management")
    obj.mo(tagAnnotation, key="niwaki-it-note", value="exhaustive-provisioning")
    obj.mo(aaaRbacAnnotation, domain="all")


def test_oob_epgs(live_aci: Niwaki) -> None:
    mgmt = tenant(TN)

    flt = mgmt.filter(FILTER, description="OOB service filter.")
    flt.entry("ssh", tcp=22, ethernet_type="ipv4", protocol="tcp", description="SSH.")
    _common(flt)

    oob_ctr = mgmt.oob_contract(
        CONTRACT, scope="context", description="Out-of-band service contract."
    )
    oob_ctr.subject("oob", description="OOB access subject.").bind(filter=FILTER)
    _common(oob_ctr)

    profile = mgmt.management_profile()
    entity = mgmt.external_management_entity()

    for i, prio in enumerate(PRIOS):
        oob = profile.out_of_band_epg(
            OOB_NAMES[i], qos_class=prio, description=f"Out-of-band EPG per QoS class, {prio}."
        )
        oob.provide(CONTRACT)
        _common(oob)

        ext = entity.external_management_epg(
            EXT_NAMES[i],
            qos_class=prio,
            description=f"External management network per QoS class, {prio}.",
        )
        ext.external_subnet(f"10.60.{i}.0/24", description="Management source subnet.")
        ext.external_subnet(f"10.61.{i}.0/24", description="Second management source subnet.")
        ext.consume(CONTRACT)
        _common(ext)

    mgmt.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    dns = [f"uni/tn-{TN}/mgmtp-default/oob-{name}" for name in OOB_NAMES]
    dns += [f"uni/tn-{TN}/extmgmt-default/instp-{name}" for name in EXT_NAMES]
    dns += [f"uni/tn-{TN}/oobbrc-{CONTRACT}", f"uni/tn-{TN}/flt-{FILTER}"]
    for dn in dns:
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
