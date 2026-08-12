"""Gate A (offline) — to_design() round-trips the golden designs byte-equal.

2.0 it.2 lot B exit criterion: for each golden design of the walkthrough
worlds, ``design → payload → pseudo-snapshot → to_design → payload`` produces
the **same wire payload** (canonicalised: child order is a declaration
artefact, sorted on both sides; attribute keys sorted by json).  The goldens
are the hardest curated positions of the DSL — contracts (vzAny, labels,
terms, exceptions), the EPG/ESG world (uSeg criteria, selectors, static
endpoints), observability (NetFlow, SPAN, QoS requirements, cross-domain
binds), the L2 edge and management EPGs, and the three-domain walkthrough.

The live half of Gate A (``snapshot.take`` on the sim → ``to_design`` →
``push(mode="plan")`` → no changes) runs in the integration suite.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from niwaki.design import Cursor, design
from tests.design.test_golden_contracts import contract_world
from tests.design.test_golden_edge_and_management import edge_and_management
from tests.design.test_golden_epg_world import epg_world
from tests.design.test_golden_observability import observability
from tests.design.test_import import canonical, pseudo_snapshot, round_trip, to_design


def three_acts() -> Cursor:
    """The multi-domain walkthrough — fabric, access, tenant, cross-domain binds."""
    cfg = design()
    fb = cfg.fabric()
    fb.datetime_policy("niwaki-datetime").ntp_provider("10.10.10.1", preferred_state=True)
    fb.vpc_protection().vpc_pair("vpc-101-102", logical_pair_id=101).node(101).node(102)
    inf = cfg.infra()
    inf.vlan_pool("niwaki-vlans", "static").range("vlan-100", "vlan-199")
    cfg.phys_dom("niwaki-phys").bind(vlan_pool="niwaki-vlans")
    inf.aaep("niwaki-aaep").bind(domain="niwaki-phys")
    t = cfg.tenant("niwaki-prod")
    t.vrf("main")
    t.bd("web").bind(vrf="main").subnet("10.0.1.1/24")
    t.app("shop").epg("web").bind(bd="web", domain="niwaki-phys")
    return cfg


GOLDENS: list[Callable[[], Cursor]] = [
    contract_world,
    epg_world,
    observability,
    edge_and_management,
    three_acts,
]


@pytest.mark.parametrize("builder", GOLDENS, ids=lambda fn: fn.__name__)
def test_golden_round_trips_byte_equal(builder: Callable[[], Cursor]) -> None:
    original, reimported = round_trip(builder())
    assert original == reimported


@pytest.mark.parametrize("builder", GOLDENS, ids=lambda fn: fn.__name__)
def test_reimport_is_a_fixed_point(builder: Callable[[], Cursor]) -> None:
    """Importing what an import produced changes nothing — the trip converges."""
    first = to_design(pseudo_snapshot(builder().to_payload()))
    second = to_design(pseudo_snapshot(first.to_payload()))
    assert canonical(second.to_payload()) == canonical(first.to_payload())
