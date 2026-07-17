"""Observability — fabric and common monitoring policies (non-prod).

Run:
    uv run pytest tests/integration/08_observability/test_003_monitoring_fabric.py -m integration -s

The operator builds a fabric monitoring policy (with a target carrying the
severity-assignment policies and every notification source), the fabric-wide
common monitoring policy (named sources and severity policies added in place), and
a spread of switch health-score retention policies over their size/purge-window
value space.

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
#   Under monFabricPol/monFabricTarget/monCommonPol: hierarchical_statistics_collection_policy
#   (statsHierColl), statistics_policy (statsReportable), pol (healthPol), the eqptdiagp_*/
#   svccoreNodePol diagnostics sets, export_policy_of_an_user (statsExportP), and (on common)
#   syslog_legacy_message_rate_limiter (syslogRateLimitP) / system_messages_policy
#   (syslogSystemMsgP) are uncurated. Universal MO children globally have no maker.

MON = "niwaki-it-mon-fabric"
SW_HEALTH = "niwaki-it-swhealth"

# Fault-severity initial must be warning or higher; target must be >= initial.
LADDER = ("warning", "minor", "major", "critical")
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
# Switch health-score retention over its size/purge-window space (purge >= 100).
HEALTH_SIZES = (2000, 5000, 10000)
HEALTH_PURGES = (100, 250, 500)


def _ordered_pairs() -> list[tuple[str, str]]:
    """Every (initial, target) pair on the ladder with target rank >= initial rank."""
    return [(init, LADDER[j]) for i, init in enumerate(LADDER) for j in range(i, len(LADDER))]


def test_fabric_monitoring(live_aci: Niwaki) -> None:
    """A fabric monitoring policy: target severities and every notification source."""
    cfg = design()
    mon = cfg.fabric().mon_fabric_monitoring_policy(
        MON,
        description="Fabric monitoring policy exercising the severity value space.",
        annotation="orchestrator:niwaki-it",
    )
    target = mon.monitoring_target(
        "l1PhysIf", description="Fabric monitoring target scoped to physical interfaces."
    )

    code = 1
    for weight, (initial, tgt) in enumerate(_ordered_pairs()):
        target.fault_severity_assignment_policy(
            code,
            description=f"Fault {code}: initial {initial}, target {tgt}.",
            initial_severity=initial,
            target_severity=tgt,
            health_score_weight=(weight * 7) % 101,
        )
        code += 1
    for i, initial in enumerate(INIT_SEVERITIES):
        target.event_severity_assignment_policy(
            600 + i,
            description=f"Event {600 + i}: initial severity {initial}.",
            initial_severity=initial,
        )

    for sev in SYSLOG_SEVERITIES:
        target.syslog_source(
            f"sl-{sev}",
            description=f"Fabric target syslog source, severity {sev}.",
            min_severity=sev,
        )
    target.snmp_source("snmp", description="Fabric target SNMP source.", min_severity="major")
    target.callhome_source(
        "ch", description="Fabric target callhome source.", message_severity="critical"
    )
    target.smart_callhome_source(name="smartch", description="Fabric target smart callhome source.")
    target.tacacs_source(
        "tac",
        description="Fabric target TACACS source.",
        min_sev="warning",
        switch_tacacs_audit="enabled",
    )

    mon.syslog_source("pol-sl", description="Fabric policy syslog source.", min_severity="alerts")
    mon.lifecycle_policy(0, description="Generic fabric fault lifecycle.", retain=3600)
    mon.stats_limit_pol(description="Fabric stats instance limit.", instance_limit=514)

    cfg.push(live_aci)


def test_common_monitoring(live_aci: Niwaki) -> None:
    """Named sources and severity policies on the fabric-wide common singleton."""
    cfg = design()
    common = cfg.fabric().mon_common_monitoring_policy()

    for sev in SYSLOG_SEVERITIES:
        common.syslog_source(
            f"niwaki-it-common-sl-{sev}",
            description=f"Common syslog source, severity {sev}.",
            min_severity=sev,
        )
    common.snmp_source(
        "niwaki-it-common-snmp", description="Common SNMP source.", min_severity="info"
    )
    common.callhome_source(
        "niwaki-it-common-ch", description="Common callhome source.", message_severity="notice"
    )
    common.smart_callhome_source(
        name="niwaki-it-common-smartch", description="Common smart callhome source."
    )
    common.tacacs_source(
        "niwaki-it-common-tac",
        description="Common TACACS source.",
        min_sev="minor",
        switch_tacacs_audit="disabled",
    )
    # Severity assignments (keyed by code) on the common policy.
    for i, (initial, tgt) in enumerate(_ordered_pairs()):
        common.fault_severity_assignment_policy(
            700 + i,
            description=f"Common fault {700 + i}: initial {initial}, target {tgt}.",
            initial_severity=initial,
            target_severity=tgt,
        )
    for i, initial in enumerate(INIT_SEVERITIES):
        common.event_severity_assignment_policy(
            800 + i,
            description=f"Common event {800 + i}: initial severity {initial}.",
            initial_severity=initial,
        )
    common.stats_limit_pol(description="Common stats instance limit.", instance_limit=520)

    cfg.push(live_aci)


def test_switch_health_retention(live_aci: Niwaki) -> None:
    """Switch health-score retention over its size/purge-window value space."""
    cfg = design()
    fab = cfg.fabric()
    for size in HEALTH_SIZES:
        for purge in HEALTH_PURGES:
            fab.switch_health_retention_policy(
                f"{SW_HEALTH}-{size}-{purge}",
                description=f"Switch health retention: {size} max, {purge} purge window.",
                maximum_size=size,
                purge_window_size=purge,
            )
    cfg.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    dns = [f"uni/fabric/monfab-{MON}"]
    # Named children on the common monitoring singleton (kept in place).
    for sev in SYSLOG_SEVERITIES:
        dns.append(f"uni/fabric/moncommon/slsrc-niwaki-it-common-sl-{sev}")
    dns += [
        "uni/fabric/moncommon/snmpsrc-niwaki-it-common-snmp",
        "uni/fabric/moncommon/chsrc-niwaki-it-common-ch",
        "uni/fabric/moncommon/smartchsrc",
        "uni/fabric/moncommon/tacacssrc-niwaki-it-common-tac",
        "uni/fabric/moncommon/limitpol",
    ]
    for i in range(len(_ordered_pairs())):
        dns.append(f"uni/fabric/moncommon/fsevp-{700 + i}")
    for i in range(len(INIT_SEVERITIES)):
        dns.append(f"uni/fabric/moncommon/esevp-{800 + i}")
    for size in HEALTH_SIZES:
        for purge in HEALTH_PURGES:
            dns.append(f"uni/fabric/swhretp-{SW_HEALTH}-{size}-{purge}")
    for dn in dns:
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
