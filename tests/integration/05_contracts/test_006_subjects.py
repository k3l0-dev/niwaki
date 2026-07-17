"""Tenant contracts — subjects, terminals and filter bindings (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/05_contracts/test_006_subjects.py -m integration -s

The ``vzSubj`` surface driven to the corners:

* the provider-label x consumer-label match-type cartesian (every VzMatchT value on
  each side), with rotating QoS class and subject-level DSCP;
* both directions — apply-both-ways (``reverse_filter_ports=True`` with a subject
  filter binding) and one-way (``reverse_filter_ports=False`` with in/out terminals,
  each varying its QoS class and terminal DSCP);
* the filter-binding (``vzRsSubjFiltAtt``) configured through ``ref()`` across the
  cartesian of action (permit / deny) x directives (none / log / no-stats / both) x
  priority-override (every VzPriorityLevel).

Values are illustrative — this proves the SDK expresses the subject surface, not a
production policy. ``wipe(aci)`` (operator-only) removes what this file owns.
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

FLT = "niwaki-it-subj-flt"
CTR_MATCH = "niwaki-it-subj-match"
CTR_TERMS = "niwaki-it-subj-terms"
CTR_BIND = "niwaki-it-subj-bind"
CTR_DSCP = "niwaki-it-subj-dscp"

MATCH_TYPES = ("All", "AtleastOne", "AtmostOne", "None")
QOS = ("level1", "level2", "level3", "level4", "level5", "level6", "unspecified")
SUBJ_DSCP = ("AF11", "AF21", "AF31", "AF41", "CS2", "EF", "unspecified")
# Every subject-/terminal-level DSCP value (named-number), swept one object each.
DSCP_ALL = (
    "AF11", "AF12", "AF13", "AF21", "AF22", "AF23", "AF31", "AF32", "AF33", "AF41", "AF42", "AF43",
    "CS0", "CS1", "CS2", "CS3", "CS4", "CS5", "CS6", "CS7", "EF", "VA", "unspecified",
)  # fmt: skip
BIND_ACTIONS = ("permit", "deny")
BIND_DIRECTIVES = ("none", "log", "no_stats", "log,no_stats")
BIND_PRIORITIES = ("default", "level1", "level2", "level3")


def _match_abbr(m: str) -> str:
    return {"All": "all", "AtleastOne": "atl", "AtmostOne": "atm", "None": "none"}[m]


def test_subject_match_types(live_aci: Niwaki) -> None:
    # provider-match x consumer-match cartesian, rotating QoS and subject DSCP.
    cfg = tenant(TN, description=TN_DESC)
    flt = cfg.filter(FLT, description="Shared filter for the subject cartesian.")
    flt.entry("e-https", tcp=443, description="HTTPS.")

    ctr = cfg.contract(CTR_MATCH, scope="context", description="Contract for match-type subjects.")
    n = 0
    for pm in MATCH_TYPES:
        for cm in MATCH_TYPES:
            subj = ctr.subject(
                f"subj-{_match_abbr(pm)}-{_match_abbr(cm)}",
                reverse_filter_ports=True,
                provider_label_match_type=pm,
                consumer_label_match_type=cm,
                qos_class_id=QOS[n % len(QOS)],
                subject_level_dscp=SUBJ_DSCP[n % len(SUBJ_DSCP)],
                description=f"Subject provider-match {pm} consumer-match {cm}.",
            )
            subj.bind(filter=FLT)
            n += 1
    cfg.push(live_aci)


def test_subject_terminals(live_aci: Niwaki) -> None:
    # One-way subjects: in/out terminals across every QoS class and rotating DSCP.
    cfg = tenant(TN, description=TN_DESC)
    cfg.filter(FLT, description="Shared filter for the subject cartesian.").entry(
        "e-https", tcp=443, description="HTTPS."
    )
    ctr = cfg.contract(CTR_TERMS, scope="context", description="Contract for one-way subjects.")
    for i, qos in enumerate(QOS):
        subj = ctr.subject(
            f"subj-oneway-{qos}",
            reverse_filter_ports=False,
            description=f"One-way subject, terminals at qos {qos}.",
        )
        subj.in_term(
            qos_class_id=qos,
            terminal_level_dscp=SUBJ_DSCP[i % len(SUBJ_DSCP)],
            description="Consumer-to-provider terminal.",
        ).bind(filter=FLT)
        subj.out_term(
            qos_class_id=QOS[-1 - i],
            terminal_level_dscp=SUBJ_DSCP[-1 - i],
            description="Provider-to-consumer terminal.",
        ).bind(filter=FLT)
    cfg.push(live_aci)


def test_filter_binding_directives(live_aci: Niwaki) -> None:
    # The subject filter binding (vzRsSubjFiltAtt) configured through ref(): the
    # cartesian of action x directives x priority-override, one subject per combo.
    cfg = tenant(TN, description=TN_DESC)
    cfg.filter(FLT, description="Shared filter for the subject cartesian.").entry(
        "e-https", tcp=443, description="HTTPS."
    )
    ctr = cfg.contract(CTR_BIND, scope="context", description="Contract for filter-binding combos.")
    n = 0
    for action in BIND_ACTIONS:
        for directives in BIND_DIRECTIVES:
            for prio in BIND_PRIORITIES:
                subj = ctr.subject(
                    f"subj-bind-{n:03d}",
                    reverse_filter_ports=True,
                    description=f"Filter binding action {action} prio {prio}.",
                )
                subj.bind(
                    filter=ref(FLT, action=action, directives=directives, priority_override=prio)
                )
                n += 1
    cfg.push(live_aci)


def test_subject_terminal_dscp_sweep(live_aci: Niwaki) -> None:
    # Every subject-level DSCP (on apply-both subjects) and every terminal-level DSCP
    # (on one-way subjects' in/out terminals).
    cfg = tenant(TN, description=TN_DESC)
    cfg.filter(FLT, description="Shared filter for the subject cartesian.").entry(
        "e-https", tcp=443, description="HTTPS."
    )
    ctr = cfg.contract(CTR_DSCP, scope="context", description="Contract for the DSCP sweep.")
    for dscp in DSCP_ALL:
        both = ctr.subject(
            f"subj-sdscp-{dscp.lower()}",
            reverse_filter_ports=True,
            subject_level_dscp=dscp,
            description=f"Subject-level DSCP {dscp}.",
        )
        both.bind(filter=FLT)
        oneway = ctr.subject(
            f"subj-tdscp-{dscp.lower()}",
            reverse_filter_ports=False,
            description=f"Terminal-level DSCP {dscp}.",
        )
        oneway.in_term(terminal_level_dscp=dscp, description="In terminal.").bind(filter=FLT)
        oneway.out_term(terminal_level_dscp=dscp, description="Out terminal.").bind(filter=FLT)
    cfg.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (
        f"uni/tn-{TN}/brc-{CTR_MATCH}",
        f"uni/tn-{TN}/brc-{CTR_TERMS}",
        f"uni/tn-{TN}/brc-{CTR_BIND}",
        f"uni/tn-{TN}/brc-{CTR_DSCP}",
        f"uni/tn-{TN}/flt-{FLT}",
    ):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
