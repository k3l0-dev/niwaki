"""Tenant contracts — contract attributes, scope x QoS cartesian (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/05_contracts/test_005_contracts.py -m integration -s

The ``vzBrCP`` attribute surface, driven to the corners: one contract for every
(scope x QoS-class) combination — all four scopes crossed with all seven tenant QoS
priorities — each carrying a rotating contract-level DSCP and a minimal permit
subject. A second pass exercises the admin ``intent`` values the APIC accepts on a
create (install and estimate-add; estimate-delete is refused unless already
installed, so it is left out).

Values are illustrative — this proves the SDK expresses the contract surface, not a
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

FLT = "niwaki-it-ctr-flt"
INTENT_CTRS = ("niwaki-it-ctr-intent-install", "niwaki-it-ctr-intent-estimate")

SCOPES = ("application-profile", "context", "global", "tenant")
SCOPE_ABBR = {"application-profile": "ap", "context": "ctx", "global": "glob", "tenant": "tn"}
QOS = ("level1", "level2", "level3", "level4", "level5", "level6", "unspecified")
CTR_DSCP = ("AF11", "AF31", "CS3", "CS6", "EF", "VA", "unspecified")
# Every contract-level DSCP value (named-number), swept one contract each.
DSCP_ALL = (
    "AF11", "AF12", "AF13", "AF21", "AF22", "AF23", "AF31", "AF32", "AF33", "AF41", "AF42", "AF43",
    "CS0", "CS1", "CS2", "CS3", "CS4", "CS5", "CS6", "CS7", "EF", "VA", "unspecified",
)  # fmt: skip


def _scope_qos_name(scope: str, qos: str) -> str:
    return f"niwaki-it-ctr-{SCOPE_ABBR[scope]}-{qos}"


def _dscp_name(dscp: str) -> str:
    return f"niwaki-it-ctr-dscp-{dscp.lower()}"


def test_scope_qos_cartesian(live_aci: Niwaki) -> None:
    # One contract per (scope x QoS) pair, each with a rotating contract-level DSCP
    # and a permit subject that binds the shared filter.
    cfg = tenant(TN, description=TN_DESC)
    flt = cfg.filter(FLT, description="Shared filter for the contract cartesian.")
    flt.entry("e-https", tcp=443, description="HTTPS.")

    n = 0
    for scope in SCOPES:
        for qos in QOS:
            ctr = cfg.contract(
                _scope_qos_name(scope, qos),
                scope=scope,
                qos_class_id=qos,
                contract_level_dscp=CTR_DSCP[n % len(CTR_DSCP)],
                description=f"Contract scope {scope} qos {qos}.",
            )
            ctr.subject("subj", reverse_filter_ports=True, description="Permit subject.").bind(
                filter=FLT
            )
            n += 1
    cfg.push(live_aci)


def test_contract_intents(live_aci: Niwaki) -> None:
    # The admin intent values accepted on a fresh contract.
    cfg = tenant(TN, description=TN_DESC)
    cfg.filter(FLT, description="Shared filter for the contract cartesian.").entry(
        "e-https", tcp=443, description="HTTPS."
    )
    cfg.contract(
        INTENT_CTRS[0], scope="context", intent="install", description="Intent install."
    ).subject("subj", reverse_filter_ports=True, description="Subject.").bind(filter=FLT)
    cfg.contract(
        INTENT_CTRS[1], scope="context", intent="estimate_add", description="Intent estimate-add."
    ).subject("subj", reverse_filter_ports=True, description="Subject.").bind(filter=FLT)
    cfg.push(live_aci)


def test_contract_dscp_sweep(live_aci: Niwaki) -> None:
    # One contract per contract-level DSCP value (every named-number value).
    cfg = tenant(TN, description=TN_DESC)
    cfg.filter(FLT, description="Shared filter for the contract cartesian.").entry(
        "e-https", tcp=443, description="HTTPS."
    )
    for dscp in DSCP_ALL:
        cfg.contract(
            _dscp_name(dscp),
            scope="context",
            contract_level_dscp=dscp,
            description=f"Contract-level DSCP {dscp}.",
        ).subject("subj", reverse_filter_ports=True, description="Subject.").bind(filter=FLT)
    cfg.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    dns = [f"uni/tn-{TN}/flt-{FLT}"]
    dns += [f"uni/tn-{TN}/brc-{c}" for c in INTENT_CTRS]
    dns += [f"uni/tn-{TN}/brc-{_scope_qos_name(scope, qos)}" for scope in SCOPES for qos in QOS]
    dns += [f"uni/tn-{TN}/brc-{_dscp_name(dscp)}" for dscp in DSCP_ALL]
    for dn in dns:
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
