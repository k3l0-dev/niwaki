"""L4-L7 services — function nodes and connectors, combination coverage (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/07_services/test_002_function_nodes.py -m integration -s

Under one abstract graph the operator lays down a function node per function-template
type, sweeping the node function type, routing mode, ``managed`` / ``share_encap``
flags, plus a dedicated unmanaged node and a copy node. Each managed node carries two
function connectors so every ``vnsFuncConnType`` value and both ``att_notify`` states
are exercised, and one connector carries the abstract folder / param / relation config
model. Managed nodes bind a local virtual logical device so the closed world resolves.

Combination sweep — not a production graph. ``wipe(aci)`` (operator-only) drops the
dedicated tenant, which cascades everything.

Universal children (``tagInst`` / ``tagExtMngdInst``) carry no typed maker in the DSL.

# COVERAGE GAPS (curated children with no reachable maker/bind — reported, not forced):
#   - maker:vnsAbsDevCfg@vnsAbsNode / vnsAbsFuncCfg@vnsAbsNode / vnsAbsGrpCfg@vnsAbsNode
#   - bind:vnsRsNodeToMFunc / vnsRsNodeToAbsFuncProf / vnsRsNodeToCloudLDev /
#     vnsRsDefaultScopeToTerm @vnsAbsNode
#   - bind:vnsRsConnToAConn / vnsRsConnToCtxTerm / vnsRsMConnAtt @vnsAbsFuncConn
#   - maker:vnsFolderInst / vnsParamInst / vnsCfgRelInst / vnsAbsCfgRel @vnsAbsFuncConn
#   - maker:vnsAbsFolder@vnsAbsFolder (folder-in-folder — one level only) plus
#     bind:vnsRsCfgToConn / vnsRsScopeToTerm @vnsAbsFolder
"""

from __future__ import annotations

import contextlib
import itertools

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

TN = "niwaki-it-svcnode"
GRAPH = "niwaki-it-graph"
LDEV = "niwaki-it-node-ldev"
COPY_LDEV = "niwaki-it-copy-ldev"

FUNC_TEMPLATE_TYPES = (
    "ADC_ONE_ARM",
    "ADC_TWO_ARM",
    "FW_ROUTED",
    "FW_TRANS",
    "OTHER",
)
FUNCTION_TYPES = ("GoThrough", "GoTo", "L1", "L2", "None")
ROUTING_MODES = ("Redirect", "unspecified")
FUNC_CONN_TYPES = ("dnat", "none", "redir", "snat", "snat_dnat")


def test_function_nodes(live_aci: Niwaki) -> None:
    tn = tenant(TN, description="Function node and connector combination sweep.")

    # Local filter the function connectors reference (closed-world bind target).
    tn.filter("niwaki-it-node-flt", description="Filter for the function connectors.")

    # Local virtual device the managed nodes bind to, plus a copy device.
    ldev = tn.logical_device(LDEV, managed=True, device_type="VIRTUAL", function_type="GoTo")
    ldev.logical_interface("consumer", encap="vlan-900")
    ldev.logical_interface("provider", encap="vlan-901")
    # A copy device must be unmanaged, physical, and function type None.
    copy_ldev = tn.logical_device(
        COPY_LDEV,
        managed=False,
        device_type="PHYSICAL",
        function_type="None",
        is_copy=True,
        svc_type="COPY",
    )
    copy_ldev.logical_interface("copy", encap="vlan-902")

    graph = tn.service_graph(GRAPH, description="Graph hosting the function-node sweep.")

    function_cycle = itertools.cycle(FUNCTION_TYPES)
    routing_cycle = itertools.cycle(ROUTING_MODES)
    conn_cycle = itertools.cycle(FUNC_CONN_TYPES)

    # One managed node per function-template type; sweep function type / routing / flags.
    for index, template in enumerate(FUNC_TEMPLATE_TYPES):
        node = graph.function_node(
            f"NODE{index:02d}",
            description=f"Managed node ({template}).",
            func_template_type=template,
            function_type=next(function_cycle),
            managed=True,
            is_copy=False,
            routing_mode=next(routing_cycle),
            sequence_number=index + 1,
            share_encap=bool(index % 2),
        )
        node.bind(logical_device=LDEV)
        consumer = node.function_connector(
            "consumer",
            description="Consumer connector.",
            conn_type=next(conn_cycle),
            att_notify=bool(index % 2),
        )
        consumer.bind(filter="niwaki-it-node-flt")
        node.function_connector(
            "provider",
            description="Provider connector.",
            conn_type=next(conn_cycle),
            att_notify=not bool(index % 2),
        )
        # The abstract folder / param / relation config model on the first node.
        if index == 0:
            folder = consumer.folder(
                "svc-folder",
                aux_info="firewall",
                cardinality="n",
                dev_ctx_lbl="fw",
                key="ExternalIf",
                locked=False,
                profile_behavior_shared=False,
                scoped_by="epg",
            )
            folder.param("iface", key="name", value="outside", cardinality="1", mandatory=True)
            folder.relation("peer", key="rel", target_name="inside", cardinality="n")
            consumer.param("conn-mtu", key="mtu", value="1500", cardinality="1")

    # Unmanaged node — covers managed=False.
    unmanaged = graph.function_node(
        "NODE-UNMANAGED",
        description="Unmanaged node.",
        func_template_type="OTHER",
        function_type="GoTo",
        managed=False,
        is_copy=False,
        routing_mode="unspecified",
        sequence_number=90,
        share_encap=True,
    )
    unmanaged.function_connector("consumer", conn_type="none", att_notify=False)

    # Copy node — covers is_copy=True and the copy connector.
    copy_node = graph.function_node(
        "NODE-COPY",
        description="Copy node for a copy service.",
        func_template_type="OTHER",
        function_type="GoTo",
        managed=False,
        is_copy=True,
        sequence_number=91,
    )
    copy_node.bind(logical_device=COPY_LDEV)
    copy_node.copy_connector(name="copy", description="Copy connector.", att_notify=True)

    tn.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (f"uni/tn-{TN}",):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
