"""Tenant contracts — taboo contracts and deny rules (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/05_contracts/test_008_taboo.py -m integration -s

A taboo contract is explicit deny. Its subjects (``vzTSubj``) hang deny rules — a
filter binding that resolves to ``vzRsDenyRule`` — and this file exercises every
directives combination that binding carries (none / log / no-stats / both) over
several deny filters, plus a subject that denies multiple filters at once.

Values are illustrative — this proves the SDK expresses the taboo surface, not a
production deny policy. ``wipe(aci)`` (operator-only) removes what this file owns.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import ref, tenant
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

TN = "niwaki-it-contracts"
TN_DESC = "Exhaustive contract, filter, label, taboo, vzAny and QoS surface"

TABOO = "niwaki-it-taboo"
TABOO_FLT_A = "niwaki-it-taboo-flt-a"
TABOO_FLT_B = "niwaki-it-taboo-flt-b"

DIRECTIVES = ("none", "log", "no_stats", "log,no_stats")


def test_taboo_deny_rules(live_aci: Niwaki) -> None:
    cfg = tenant(TN, description=TN_DESC)

    flt_a = cfg.filter(TABOO_FLT_A, description="First deny filter.")
    flt_a.entry("e-telnet", tcp=23, description="Telnet, denied.")
    flt_a.entry("e-tftp", udp=69, description="TFTP, denied.")
    flt_b = cfg.filter(TABOO_FLT_B, description="Second deny filter.")
    flt_b.entry("e-snmp", udp=161, description="SNMP, denied.")

    taboo = cfg.taboo_contract(TABOO, description="Explicit-deny (taboo) contract.")

    # One subject per directives combination, denying the first filter.
    for i, directives in enumerate(DIRECTIVES):
        subj = taboo.subject(f"subj-dir-{i}", description=f"Deny rule directives {directives}.")
        subj.bind(filter=ref(TABOO_FLT_A, directives=directives))

    # A subject that denies several filters at once (two deny rules).
    multi = taboo.subject("subj-multi", description="Deny multiple filters at once.")
    multi.bind(filter=ref(TABOO_FLT_A, directives="log"))
    multi.bind(filter=ref(TABOO_FLT_B, directives="no_stats"))

    cfg.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (
        f"uni/tn-{TN}/taboo-{TABOO}",
        f"uni/tn-{TN}/flt-{TABOO_FLT_A}",
        f"uni/tn-{TN}/flt-{TABOO_FLT_B}",
    ):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
