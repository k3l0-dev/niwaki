"""L4-L7 services — abstract graph templates, combination coverage (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/07_services/test_001_service_graph_templates.py -m integration

The operator stamps out one abstract service-graph template (``vnsAbsGraph``) per
UI template type, cycling the service-rule scope and the between-node filter policy so
every value of every ``vnsAbsGraph`` enum is exercised at least once. Node / connector
/ terminal coverage lives in the sibling files (function nodes, connections/terminals).

This is a combination sweep to prove the SDK expresses every graph-level knob — not a
production catalogue. Values are illustrative. ``wipe(aci)`` (operator-only) drops the
dedicated tenant, which cascades every graph.

Universal children (``tagInst`` / ``tagExtMngdInst``) carry no typed maker in the DSL
and are not configured here.
"""

from __future__ import annotations

import contextlib
import itertools

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

TN = "niwaki-it-svcgraph"

# Every value of every vnsAbsGraph enum, swept below.
UI_TEMPLATE_TYPES = (
    "ONE_NODE_ADC_ONE_ARM",
    "ONE_NODE_ADC_ONE_ARM_L3EXT",
    "ONE_NODE_ADC_TWO_ARM",
    "ONE_NODE_FW_ROUTED",
    "ONE_NODE_FW_TRANS",
    "TWO_NODE_FW_ROUTED_ADC_ONE_ARM",
    "TWO_NODE_FW_ROUTED_ADC_ONE_ARM_L3EXT",
    "TWO_NODE_FW_ROUTED_ADC_TWO_ARM",
    "TWO_NODE_FW_TRANS_ADC_ONE_ARM",
    "TWO_NODE_FW_TRANS_ADC_ONE_ARM_L3EXT",
    "TWO_NODE_FW_TRANS_ADC_TWO_ARM",
    "UNSPECIFIED",
)
SVC_RULE_TYPES = ("epg", "subnet", "vrf")
FILTER_BETWEEN_NODES = ("allow-all", "filters-from-contract")


def test_service_graph_templates(live_aci: Niwaki) -> None:
    tn = tenant(TN, description="Abstract graph template sweep: UI-template x scope x filter.")

    # Full cartesian: UI template type x service-rule scope x between-node filter.
    for index, (ui_template, svc_rule, filter_between) in enumerate(
        itertools.product(UI_TEMPLATE_TYPES, SVC_RULE_TYPES, FILTER_BETWEEN_NODES)
    ):
        tn.service_graph(
            f"niwaki-it-g{index:02d}",
            description=f"Template {ui_template} scope {svc_rule}.",
            ui_template_type=ui_template,
            svc_rule_type=svc_rule,
            filter_between_nodes=filter_between,
            type="legacy",
            owner_key="svc",
            owner_tag="itlab",
        )

    tn.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (f"uni/tn-{TN}",):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
