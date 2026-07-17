"""Tenant contracts — vzAny attributes, provide/consume and imports (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/05_contracts/test_009_vzany.py -m integration -s

``vzAny`` hangs contracts once for every EPG in a VRF. Since it is a singleton per
VRF, the (match-type x preferred-group-member) cartesian is spread across one VRF per
combination. A second pass wires a vzAny to several provided and consumed contracts
through its own Rs verbs and references an imported contract.

Values are illustrative — this proves the SDK expresses the vzAny surface, not a
production policy. ``wipe(aci)`` (operator-only) removes what this file owns.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

TN = "niwaki-it-contracts"
TN_DESC = "Exhaustive contract, filter, label, taboo, vzAny and QoS surface"

MATCH_TYPES = ("All", "AtleastOne", "AtmostOne", "None")
PREF = ("disabled", "enabled")
MATCH_ABBR = {"All": "all", "AtleastOne": "atl", "AtmostOne": "atm", "None": "none"}

# vzAny contract-wiring pass.
WIRE_VRF = "niwaki-it-vzany-wire-vrf"
WIRE_FLT = "niwaki-it-vzany-flt"
PROVIDED = ("niwaki-it-vzany-prov1", "niwaki-it-vzany-prov2")
CONSUMED = ("niwaki-it-vzany-cons1", "niwaki-it-vzany-cons2")
EXPORTED = "niwaki-it-vzany-exp"
IMPORTED = "niwaki-it-vzany-cif"


def _vrf_name(match: str, pref: str) -> str:
    return f"niwaki-it-vzany-{MATCH_ABBR[match]}-{pref}"


def test_vzany_match_cartesian(live_aci: Niwaki) -> None:
    # One VRF per (match-type x preferred-group-member); each VRF's vzAny carries the
    # combination.
    cfg = tenant(TN, description=TN_DESC)
    for match in MATCH_TYPES:
        for pref in PREF:
            cfg.vrf(_vrf_name(match, pref), description=f"VRF for vzAny {match}/{pref}.").vzany(
                match_type=match,
                preferred_group_member=pref,
                description=f"vzAny match {match} pref-group {pref}.",
            )
    cfg.push(live_aci)


def test_vzany_contracts(live_aci: Niwaki) -> None:
    # A vzAny that provides and consumes several contracts and references an import.
    cfg = tenant(TN, description=TN_DESC)
    cfg.filter(WIRE_FLT, description="Filter for the vzAny contracts.").entry(
        "e-https", tcp=443, description="HTTPS."
    )
    for name in (*PROVIDED, *CONSUMED):
        cfg.contract(name, scope="context", description=f"vzAny target {name}.").subject(
            "subj", reverse_filter_ports=True, description="Subject."
        ).bind(filter=WIRE_FLT)

    cfg.contract(
        EXPORTED, scope="global", description="Contract behind the vzAny interface."
    ).subject("subj", reverse_filter_ports=True, description="Subject.").bind(filter=WIRE_FLT)
    cfg.imported_contract(IMPORTED, description="Interface the vzAny consumes.").bind(
        contract=EXPORTED
    )

    vzany = cfg.vrf(WIRE_VRF, description="VRF whose vzAny wires contracts.").vzany(
        match_type="AtleastOne", description="vzAny wiring provide/consume/import."
    )
    for name in PROVIDED:
        vzany.provide(name)
    for name in CONSUMED:
        vzany.consume(name)
    vzany.bind(imported_contract=IMPORTED)

    cfg.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    dns = [f"uni/tn-{TN}/ctx-{_vrf_name(match, pref)}" for match in MATCH_TYPES for pref in PREF]
    dns.append(f"uni/tn-{TN}/ctx-{WIRE_VRF}")
    dns.append(f"uni/tn-{TN}/cif-{IMPORTED}")
    dns.append(f"uni/tn-{TN}/brc-{EXPORTED}")
    dns += [f"uni/tn-{TN}/brc-{c}" for c in (*PROVIDED, *CONSUMED)]
    dns.append(f"uni/tn-{TN}/flt-{WIRE_FLT}")
    for dn in dns:
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
