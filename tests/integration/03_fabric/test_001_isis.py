"""Fabric — IS-IS domain policies (exhaustive combinations, non-prod).

Run:
    uv run pytest tests/integration/03_fabric/test_001_isis.py -m integration -s

The fabric IS-IS domain policy governs the underlay IGP timing. This file
provisions one policy per representative ``(MTU, redistribution-metric)`` pair,
and inside each a level-1 level component (the fabric supports IS-IS level-1
only) whose LSP fast-flood is toggled both ways across the set, together with a
full spread of LSP-generation and SPF-computation timers.

Exhaustive combination coverage, illustrative values — not a real fabric config.

``wipe(aci)`` (operator-only) removes every ``niwaki-it-*`` IS-IS domain policy.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import fabric
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

PREFIX = "niwaki-it-isis"
# (mtu, redistribution metric) — MTU is 256..4352, metric is 1..63 by the schema.
MTU_METRIC = ((1492, 63), (1500, 32), (4352, 1), (4000, 20))


def test_isis_domain_policies(live_aci: Niwaki) -> None:
    fab = fabric()
    for idx, (mtu, metric) in enumerate(MTU_METRIC):
        isis = fab.isis_domain_policy(
            f"{PREFIX}-{mtu}-{metric}",
            description=f"IS-IS MTU/metric sweep ({mtu} B, metric {metric}).",
            maximum_transmission_unit=mtu,
            metric=metric,
        )
        # Level-1 only; alternate the fast-flood flag across the set.
        isis.isis_level(
            "l1",
            description="IS-IS level-1 LSP generation and SPF timers.",
            lsp_fast_flood="enabled" if idx % 2 == 0 else "disabled",
            lsp_generation_initial_wait_inerval=50 + idx,
            lsp_generation_maximal_wait_inerval=8000,
            lsp_generation_secondary_wait_inerval=50 + idx,
            spf_comp_init_intvl=50 + idx,
            spf_comp_max_intvl=8000,
            spf_comp_sec_intvl=50 + idx,
        )
    fab.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for mtu, metric in MTU_METRIC:
        with contextlib.suppress(NotFoundError):
            aci.node(f"uni/fabric/isisDomP-{PREFIX}-{mtu}-{metric}").delete()
