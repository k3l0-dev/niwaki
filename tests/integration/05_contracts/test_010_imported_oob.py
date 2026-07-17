"""Tenant contracts — imported and out-of-band contracts (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/05_contracts/test_010_imported_oob.py -m integration -s

Two more contract shapes:

* **Imported contracts** (``vzCPIf``) — consumption interfaces onto contracts exported
  elsewhere, reached both closed-world (``bind``) and by raw DN (``bind_dn``).
* **Out-of-band contracts** (``vzOOBBrCP``) — the management-plane contract across every
  scope, plus a rich one carrying both-ways and one-way subjects, subject labels,
  terminals and exceptions.

Values are illustrative — this proves the SDK expresses these contract shapes, not a
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

IMP_FLT = "niwaki-it-imp-flt"
EXPORTED = ("niwaki-it-exp-a", "niwaki-it-exp-b", "niwaki-it-exp-c")
IMPORTED = ("niwaki-it-imp-a", "niwaki-it-imp-b", "niwaki-it-imp-c")
IMPORTED_DN = "niwaki-it-imp-dn"

OOB_FLT = "niwaki-it-oob-flt"
OOB_RICH = "niwaki-it-oob-rich"
SCOPES = ("application-profile", "context", "global", "tenant")
SCOPE_ABBR = {"application-profile": "ap", "context": "ctx", "global": "glob", "tenant": "tn"}
QOS = ("level1", "level2", "level3", "level4")


def _oob_name(scope: str) -> str:
    return f"niwaki-it-oob-{SCOPE_ABBR[scope]}"


def test_imported_contracts(live_aci: Niwaki) -> None:
    # Exported contracts and the interfaces that point at them, closed-world and by DN.
    cfg = tenant(TN, description=TN_DESC)
    cfg.filter(IMP_FLT, description="Filter for the exported contracts.").entry(
        "e-https", tcp=443, description="HTTPS."
    )
    for name in EXPORTED:
        cfg.contract(name, scope="global", description=f"Exported contract {name}.").subject(
            "subj", reverse_filter_ports=True, description="Subject."
        ).bind(filter=IMP_FLT)

    for imp, exp in zip(IMPORTED, EXPORTED, strict=True):
        cfg.imported_contract(imp, description=f"Interface onto {exp}.").bind(contract=exp)
    cfg.imported_contract(IMPORTED_DN, description="Interface by raw DN.").bind_dn(
        contract=f"uni/tn-{TN}/brc-{EXPORTED[0]}"
    )
    cfg.push(live_aci)


def test_oob_scopes(live_aci: Niwaki) -> None:
    # An out-of-band contract per scope, rotating QoS class, each with a subject.
    cfg = tenant(TN, description=TN_DESC)
    cfg.filter(OOB_FLT, description="Management ports for the OOB contracts.").entry(
        "e-ssh", tcp=22, description="SSH."
    )
    for i, scope in enumerate(SCOPES):
        oob = cfg.oob_contract(
            _oob_name(scope),
            scope=scope,
            qos_class_id=QOS[i % len(QOS)],
            intent="install",
            description=f"OOB contract scope {scope}.",
        )
        oob.subject("subj", reverse_filter_ports=True, description="Subject.").bind(filter=OOB_FLT)
    cfg.push(live_aci)


def test_oob_rich(live_aci: Niwaki) -> None:
    # One OOB contract carrying both-ways and one-way subjects, labels, terminals
    # and an exception.
    cfg = tenant(TN, description=TN_DESC)
    cfg.filter(OOB_FLT, description="Management ports for the OOB contracts.").entry(
        "e-ssh", tcp=22, description="SSH."
    )
    oob = cfg.oob_contract(
        OOB_RICH,
        scope="context",
        qos_class_id="level1",
        contract_level_dscp="CS6",
        description="Rich OOB management contract.",
    )
    both = oob.subject(
        "subj-both",
        reverse_filter_ports=True,
        provider_label_match_type="All",
        consumer_label_match_type="AtleastOne",
        description="Bidirectional OOB subject.",
    )
    both.bind(filter=OOB_FLT)
    both.provider_subject_label("plbl", tag="olive", description="Provider subject label.")
    both.consumer_subject_label("clbl", tag="salmon", description="Consumer subject label.")
    both.exception("exc-epg", field="EPg", prov_regex="oob-.*")

    oneway = oob.subject(
        "subj-oneway", reverse_filter_ports=False, description="Asymmetric OOB subject."
    )
    oneway.in_term(qos_class_id="level2", description="In terminal.").bind(filter=OOB_FLT)
    oneway.out_term(qos_class_id="level3", description="Out terminal.").bind(filter=OOB_FLT)

    oob.exception("exc-tenant", field="Tenant", cons_regex="mgmt.*")
    cfg.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    dns = [f"uni/tn-{TN}/flt-{IMP_FLT}", f"uni/tn-{TN}/flt-{OOB_FLT}"]
    dns += [f"uni/tn-{TN}/brc-{c}" for c in EXPORTED]
    dns += [f"uni/tn-{TN}/cif-{c}" for c in (*IMPORTED, IMPORTED_DN)]
    dns += [f"uni/tn-{TN}/oobbrc-{_oob_name(scope)}" for scope in SCOPES]
    dns.append(f"uni/tn-{TN}/oobbrc-{OOB_RICH}")
    for dn in dns:
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
