"""Observability — infra monitoring policy, severity cartesian (non-prod).

Run:
    uv run pytest tests/integration/08_observability/test_002_monitoring_infra.py -m integration -s

The operator builds an access (infra) monitoring policy with monitoring targets and
drives the fault- and event-severity assignment policies across their full value
space: every ordered (initial, target) severity pair the APIC allows — the target
severity must be equal to or higher than the initial — plus the special
inherit/squelched values, and one event-severity assignment per initial severity.
The notification sources are also exercised under the target and at the policy
level, and the policy carries the generic lifecycle and a stats instance limit.

Exhaustive, non-prod. ``wipe(aci)`` (operator-only) removes the whole set.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import design
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

# COVERAGE GAPS (curated child in CHILD_MAP, no maker/bind/verb — reported, not forced):
#   Under monInfraTarget: hierarchical_statistics_collection_policy (statsHierColl), pol
#   (healthPol), statistics_policy (statsReportable), lifecycle_policy (faultLcP), the
#   eqptdiagp_* diagnostics sets, and export_policy_of_an_user (statsExportP) are uncurated
#   (known gaps in coverage_gaps.json). Universal MO children globally have no maker.

MON = "niwaki-it-mon-infra"

# The ordered fault-severity ladder: the initial must be warning or higher, and the
# target must be equal to or higher than the initial.
LADDER = ("warning", "minor", "major", "critical")
# The special inherit value, exercised on its own.
SPECIAL_PAIRS = (("inherit", "inherit"),)
# Every initial-severity value for event-severity assignment.
INIT_SEVERITIES = (
    "critical",
    "info",
    "inherit",
    "major",
    "minor",
    "squelched",
    "warning",
)
SYSLOG_SEVERITIES = ("alerts", "critical", "errors", "information", "notifications", "warnings")


def _ordered_pairs() -> list[tuple[str, str]]:
    """Every (initial, target) pair on the ladder with target rank >= initial rank."""
    return [(init, LADDER[j]) for i, init in enumerate(LADDER) for j in range(i, len(LADDER))]


def test_infra_monitoring_severity(live_aci: Niwaki) -> None:
    """Fault- and event-severity assignment across the full value space."""
    cfg = design()
    mon = cfg.infra().monitoring_policy(
        MON,
        description="Access monitoring policy exercising the severity value space.",
        annotation="orchestrator:niwaki-it",
    )
    target = mon.monitoring_target(
        "l1PhysIf", description="Infra monitoring target scoped to physical interfaces."
    )

    code = 1
    for weight, (initial, tgt) in enumerate(_ordered_pairs()):
        target.fault_severity_assignment_policy(
            code,
            description=f"Fault {code}: initial {initial}, target {tgt}.",
            initial_severity=initial,
            target_severity=tgt,
            health_score_weight=(weight * 5) % 101,
        )
        code += 1
    for initial, tgt in SPECIAL_PAIRS:
        target.fault_severity_assignment_policy(
            code,
            description=f"Fault {code}: initial {initial}, target {tgt}.",
            initial_severity=initial,
            target_severity=tgt,
        )
        code += 1

    ecode = 1
    for initial in INIT_SEVERITIES:
        target.event_severity_assignment_policy(
            500 + ecode,
            description=f"Event {500 + ecode}: initial severity {initial}.",
            initial_severity=initial,
        )
        ecode += 1

    cfg.push(live_aci)


def test_infra_monitoring_sources(live_aci: Niwaki) -> None:
    """Notification sources under a second target and at the policy level."""
    cfg = design()
    mon = cfg.infra().monitoring_policy(
        MON, description="Access monitoring policy notification sources."
    )

    target = mon.monitoring_target(
        "pcAggrIf", description="Infra monitoring target scoped to port-channels."
    )
    for sev in SYSLOG_SEVERITIES:
        target.syslog_source(
            f"tgt-sl-{sev}", description=f"Target syslog source, severity {sev}.", min_severity=sev
        )
    target.snmp_source("tgt-snmp", description="Target SNMP source.", min_severity="major")
    target.callhome_source(
        "tgt-ch", description="Target callhome source.", message_severity="error"
    )
    target.smart_callhome_source(name="tgt-smartch", description="Target smart callhome source.")
    target.tacacs_source(
        "tgt-tac",
        description="Target TACACS source.",
        min_sev="warning",
        switch_tacacs_audit="enabled",
    )

    # Sources at the policy level too, plus lifecycle and stats limit.
    for sev in SYSLOG_SEVERITIES:
        mon.syslog_source(
            f"pol-sl-{sev}", description=f"Policy syslog source, severity {sev}.", min_severity=sev
        )
    mon.snmp_source("pol-snmp", description="Policy SNMP source.", min_severity="minor")
    mon.callhome_source("pol-ch", description="Policy callhome source.", message_severity="notice")
    mon.tacacs_source("pol-tac", description="Policy TACACS source.", min_sev="info")
    mon.lifecycle_policy(
        0,
        description="Generic fault lifecycle.",
        clear_interval=120,
        retain=3600,
        soaking_interval=60,
    )
    mon.stats_limit_pol(
        description="Policy stats instance limit.", instance_limit=500, include_action="all"
    )

    cfg.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (f"uni/infra/moninfra-{MON}",):  # cascades targets, sources, severities
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
