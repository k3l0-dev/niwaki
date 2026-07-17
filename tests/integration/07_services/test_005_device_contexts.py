"""L4-L7 services — device contexts and interface contexts (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/07_services/test_005_device_contexts.py -m integration -s

The operator declares several device contexts (``vnsLDevCtx``, one per contract / graph
/ node selection) and, under one of them, sweeps the logical-interface contexts across
the cartesian of ``acl`` x ``l3_dest`` x ``permit_log``, rotating ``permit_handoff`` and
``rule_type``. Each interface context binds a bridge domain and carries a virtual IP;
the contexts bind local logical devices and router configs so the closed world resolves.

Combination sweep — not a production catalogue. ``wipe(aci)`` (operator-only) drops the
dedicated tenant.

Universal children (``tagInst`` / ``tagExtMngdInst``) carry no typed maker in the DSL.

# COVERAGE GAPS (curated children with no reachable maker/bind — reported, not forced):
#   - maker:fvSubnet@vnsLIfCtx
#   - bind:vnsRsLIfCtxToInstP / vnsRsLIfCtxToOut / vnsRsLIfCtxToLIf /
#     vnsRsLIfCtxToCustQosPol / vnsRsLIfCtxToSvcEPgPol / vnsRsLIfCtxToSvcRedirectPol /
#     vnsRsLIfCtxToRemoteSvcRedirectPol @vnsLIfCtx (only bridge_domain is a bind)
"""

from __future__ import annotations

import contextlib
import itertools

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

TN = "niwaki-it-devctx"

# (contract-label, graph-label, node-label, context_name)
CONTEXT_SPECS = (
    ("niwaki-it-ct-a", "niwaki-it-graph", "FW1", "prod"),
    ("niwaki-it-ct-b", "niwaki-it-graph", "ADC1", "staging"),
    ("any", "any", "any", "wildcard"),
)


def test_device_contexts(live_aci: Niwaki) -> None:
    tn = tenant(TN, description="Device context and interface-context boolean matrix.")

    # Closed-world targets the contexts and interface contexts bind to.
    tn.vrf("niwaki-it-vrf", description="VRF for the interface-context bridge domain.")
    tn.bd("niwaki-it-bd", description="Bridge domain the interface contexts point at.").bind(
        vrf="niwaki-it-vrf"
    )
    ldev = tn.logical_device("niwaki-it-ctx-ldev", managed=True, device_type="VIRTUAL")
    ldev.logical_interface("consumer", encap="vlan-910")
    ldev.logical_interface("provider", encap="vlan-911")
    tn.router_configuration(
        "niwaki-it-rtr", description="Router config for the device contexts.", rtr_id="9.9.9.9"
    )

    for index, (contract, graph, node, ctx_name) in enumerate(CONTEXT_SPECS):
        ctx = tn.logical_device_context(
            contract,
            graph,
            node,
            context_name=ctx_name,
            description=f"Device selection {ctx_name}.",
        )
        ctx.bind(logical_device="niwaki-it-ctx-ldev", router_config="niwaki-it-rtr")

        # Under the first context, sweep the full interface-context boolean cartesian
        # (acl x l3_dest x permit_handoff x permit_log x rule_type = 32 contexts).
        if index == 0:
            for lif_index, (acl, l3_dest, permit_handoff, permit_log, rule_type) in enumerate(
                itertools.product((True, False), repeat=5)
            ):
                lif = ctx.logical_interface_context(
                    f"c{lif_index:02d}",
                    acl=acl,
                    l3_dest=l3_dest,
                    permit_handoff=permit_handoff,
                    permit_log=permit_log,
                    rule_type=rule_type,
                    description=f"Interface context {lif_index:02d}.",
                )
                lif.bind(bridge_domain="niwaki-it-bd")
                lif.virtual_ip(f"10.1.{lif_index}.1", description="Interface-context VIP.")
        else:
            lif = ctx.logical_interface_context(
                "consumer", description="Consumer interface context."
            )
            lif.bind(bridge_domain="niwaki-it-bd")
            lif.virtual_ip("10.2.0.1", description="Interface-context VIP.")

    tn.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (f"uni/tn-{TN}",):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
