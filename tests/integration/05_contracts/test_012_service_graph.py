"""Tenant contracts — service-graph attachment, factored across contracts (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/05_contracts/test_012_service_graph.py -m integration -s

A contract can steer its traffic through an L4-L7 service graph. The attachment sits
either at the contract level (``vzRsGraphAtt``) or at the subject level
(``vzRsSubjGraphAtt``) — the APIC forbids both on one contract, so this file
**factors** the two: one contract carries the contract-level attachment, others carry
the subject-level one. Along the way it sweeps the abstract-graph attributes
(``filter_between_nodes`` both ways, ``svc_rule_type`` across its values).

The graphs here are shells (their nodes/devices are provisioned in phase 07); this
file proves the SDK expresses the attachment surface. Values are illustrative.
``wipe(aci)`` (operator-only) removes what this file owns.
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

SG_FLT = "niwaki-it-sg-flt"
GRAPH_A = "niwaki-it-sg-a"
GRAPH_B = "niwaki-it-sg-b"
GRAPH_C = "niwaki-it-sg-c"
CTR_GRAPH_LEVEL = "niwaki-it-ctr-graph-contract"
CTR_SUBJ_LEVEL_B = "niwaki-it-ctr-graph-subject-b"
CTR_SUBJ_LEVEL_C = "niwaki-it-ctr-graph-subject-c"


def test_service_graph_attachments(live_aci: Niwaki) -> None:
    cfg = tenant(TN, description=TN_DESC)
    cfg.filter(SG_FLT, description="Filter for the service-graph contracts.").entry(
        "e-https", tcp=443, description="HTTPS."
    )

    # Abstract-graph shells, sweeping their attribute values.
    cfg.service_graph(
        GRAPH_A,
        filter_between_nodes="allow-all",
        svc_rule_type="vrf",
        description="Graph, allow-all, VRF rule type.",
    )
    cfg.service_graph(
        GRAPH_B,
        filter_between_nodes="filters-from-contract",
        svc_rule_type="epg",
        description="Graph, filters-from-contract, EPG rule type.",
    )
    cfg.service_graph(
        GRAPH_C,
        filter_between_nodes="allow-all",
        svc_rule_type="subnet",
        description="Graph, allow-all, subnet rule type.",
    )

    # Contract-level attachment (vzRsGraphAtt) — its own contract.
    contract_level = cfg.contract(
        CTR_GRAPH_LEVEL, scope="context", description="Contract-level graph attachment."
    )
    contract_level.bind(service_graph=GRAPH_A)
    contract_level.subject("subj", reverse_filter_ports=True, description="Subject.").bind(
        filter=SG_FLT
    )

    # Subject-level attachment (vzRsSubjGraphAtt) — separate contracts, so the two
    # attachment levels never share one contract.
    for ctr_name, graph in ((CTR_SUBJ_LEVEL_B, GRAPH_B), (CTR_SUBJ_LEVEL_C, GRAPH_C)):
        ctr = cfg.contract(ctr_name, scope="context", description="Subject-level graph attachment.")
        ctr.subject(
            "subj-graph",
            reverse_filter_ports=True,
            description="Subject steered through the graph.",
        ).bind(filter=SG_FLT, service_graph=graph)

    cfg.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (
        f"uni/tn-{TN}/brc-{CTR_GRAPH_LEVEL}",
        f"uni/tn-{TN}/brc-{CTR_SUBJ_LEVEL_B}",
        f"uni/tn-{TN}/brc-{CTR_SUBJ_LEVEL_C}",
        f"uni/tn-{TN}/AbsGraph-{GRAPH_A}",
        f"uni/tn-{TN}/AbsGraph-{GRAPH_B}",
        f"uni/tn-{TN}/AbsGraph-{GRAPH_C}",
        f"uni/tn-{TN}/flt-{SG_FLT}",
    ):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
