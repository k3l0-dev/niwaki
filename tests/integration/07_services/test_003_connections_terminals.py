"""L4-L7 services — graph connections and terminals, combination coverage (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/07_services/test_003_connections_terminals.py -m integration -s

Under one abstract graph the operator sweeps the graph **connections** across the
cartesian of adjacency type x connection direction x connection type, rotating the
``direct_connect`` and ``unicast_routing`` booleans, and lays down the consumer and
provider **terminal nodes** with their in / out terminals and terminal connectors
(both ``att_notify`` states).

Combination sweep — not a production graph. ``wipe(aci)`` (operator-only) drops the
dedicated tenant.

Universal children (``tagInst`` / ``tagExtMngdInst``) carry no typed maker in the DSL.
"""

from __future__ import annotations

import contextlib
import itertools

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

TN = "niwaki-it-svcconn"
GRAPH = "niwaki-it-graph"

ADJ_TYPES = ("L2", "L3")
CONN_DIRS = ("consumer", "provider", "unknown")
CONN_TYPES = ("external", "internal")


def test_connections_and_terminals(live_aci: Niwaki) -> None:
    tn = tenant(TN, description="Graph connection and terminal combination sweep.")
    graph = tn.service_graph(GRAPH, description="Graph hosting the connection sweep.")

    bool_cycle = itertools.cycle((True, False))
    for index, (adj_type, conn_dir, conn_type) in enumerate(
        itertools.product(ADJ_TYPES, CONN_DIRS, CONN_TYPES)
    ):
        graph.connection(
            f"C{index:02d}",
            description=f"{adj_type}/{conn_dir}/{conn_type} connection.",
            adj_type=adj_type,
            conn_dir=conn_dir,
            conn_type=conn_type,
            direct_connect=next(bool_cycle),
            # L3 connections require unicast routing on; L2 connections toggle it.
            unicast_routing=True if adj_type == "L3" else next(bool_cycle),
        )

    # Consumer + provider terminal nodes, each with in / out terminals + connector.
    consumer_term = graph.consumer_terminal_node(
        "T-consumer", description="Consumer terminal node."
    )
    consumer_term.in_terminal(name="in", description="Consumer in-terminal.")
    consumer_term.out_terminal(name="out", description="Consumer out-terminal.")
    consumer_term.terminal_connector(
        name="tcon",
        description="Consumer terminal connector.",
        att_notify=True,
        device_l_if_name="consumer",
    )

    provider_term = graph.provider_terminal_node(
        "T-provider", description="Provider terminal node."
    )
    provider_term.in_terminal(name="in", description="Provider in-terminal.")
    provider_term.out_terminal(name="out", description="Provider out-terminal.")
    provider_term.terminal_connector(
        name="tcon",
        description="Provider terminal connector.",
        att_notify=False,
        device_l_if_name="provider",
    )

    tn.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (f"uni/tn-{TN}",):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
