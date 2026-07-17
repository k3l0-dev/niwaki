"""Observability — tenant EPG monitoring policy, combination coverage (non-prod).

Run:
    uv run pytest tests/integration/08_observability/test_001_monitoring_tenant.py -m integration -s

The operator builds a tenant EPG monitoring policy and drives every notification
source across every severity value the SDK exposes: one syslog source per syslog
severity, one SNMP source per condition severity, one callhome source per callhome
urgency, one TACACS source per condition severity (both switch-audit states), plus
a smart-callhome source. The include-action flag set is rotated across the sources
so every flag and several combinations are exercised. Monitoring targets cover a
spread of target-scope classes; the generic lifecycle policy (code 0, the only one
accepted here) and the statistics instance-limit policy round the policy out.

Exhaustive, non-prod: the values exist to exercise the SDK, not to model a real
monitoring baseline. ``wipe(aci)`` (operator-only) removes the whole set.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import design
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

# COVERAGE GAPS (curated child in CHILD_MAP, no maker/bind/verb — reported, not forced):
#   monEPGTarget has no sub-makers (its source/severity/stats/health children are unreachable);
#   hierarchical_statistics_collection_policy (statsHierColl), export_policy_of_an_user
#   (statsExportP), statistics_policy (statsReportable), pol (healthPol) uncurated; notification
#   sources carry no bind to their destination group (syslogRsDestGroup/snmpRsDestGroup/…).
#   Universal MO children (tag/annotation/rbac/domain-tag-ref) globally have no maker; the
#   annotation scalar is set instead.

TN = "niwaki-it-obs-mon"
TN_DESC = "Observability: tenant monitoring policies."
MON = "niwaki-it-mon-epg"
MON_MATRIX = "niwaki-it-mon-epg-matrix"

# Every severity value the SDK exposes for each source kind.
SYSLOG_SEVERITIES = (
    "alerts",
    "critical",
    "debugging",
    "emergencies",
    "errors",
    "information",
    "notifications",
    "warnings",
)
CONDITION_SEVERITIES = ("cleared", "critical", "info", "major", "minor", "warning")
CALLHOME_URGENCIES = (
    "alert",
    "critical",
    "debug",
    "emergency",
    "error",
    "info",
    "notice",
    "warning",
)
# Rotated across the sources: every flag and several combinations.
INCLUDE_ACTIONS = (
    "faults",
    "events",
    "audit",
    "session",
    "all",
    "none",
    "faults,events",
    "faults,events,audit,session",
)
# A spread of target-scope classes the monitoring policy can scope onto.
TARGET_SCOPES = ("fvBD", "fvAEPg", "fvCtx", "fvAp", "fvTenant", "fvSubnet", "fvESg")


def _incl(index: int) -> str:
    """Rotate through the include-action flag combinations."""
    return INCLUDE_ACTIONS[index % len(INCLUDE_ACTIONS)]


def test_tenant_monitoring_sources(live_aci: Niwaki) -> None:
    """Every source kind over every severity, with rotating include-action flags."""
    cfg = design()
    mon = cfg.tenant(TN, description=TN_DESC).monitoring_policy(
        MON,
        description="Tenant EPG monitoring policy exercising every source severity.",
        annotation="orchestrator:niwaki-it",
    )

    for i, sev in enumerate(SYSLOG_SEVERITIES):
        mon.syslog_source(
            f"sl-{sev}",
            description=f"Syslog source, minimum severity {sev}.",
            min_severity=sev,
            include_action=_incl(i),
        )
    for i, sev in enumerate(CONDITION_SEVERITIES):
        mon.snmp_source(
            f"snmp-{sev}",
            description=f"SNMP source, minimum severity {sev}.",
            min_severity=sev,
            include_action=_incl(i + 1),
        )
    for i, urg in enumerate(CALLHOME_URGENCIES):
        mon.callhome_source(
            f"ch-{urg}",
            description=f"Callhome source, urgency {urg}.",
            message_severity=urg,
            include_action=_incl(i + 2),
        )
    for i, sev in enumerate(CONDITION_SEVERITIES):
        mon.tacacs_source(
            f"tac-{sev}",
            description=f"TACACS source, minimum severity {sev}.",
            min_sev=sev,
            # Both switch-audit states are exercised across the loop.
            switch_tacacs_audit="enabled" if i % 2 else "disabled",
            include_action=_incl(i + 3),
        )
    mon.smart_callhome_source(name="smartch", description="Smart callhome source.")

    cfg.push(live_aci)


def test_tenant_monitoring_targets(live_aci: Niwaki) -> None:
    """Monitoring targets over a spread of scopes, plus lifecycle and stats limit."""
    cfg = design()
    mon = cfg.tenant(TN, description=TN_DESC).monitoring_policy(
        MON, description="Tenant EPG monitoring targets and lifecycle."
    )

    for scope in TARGET_SCOPES:
        mon.target(scope, description=f"Monitoring target scoped to {scope}.")

    # Only the generic lifecycle policy (code 0) is accepted under a monitoring policy.
    mon.lifecycle_policy(
        0,
        description="Generic fault lifecycle: 180 s clear, 90 s soak, 2 h retain.",
        clear_interval=180,
        soaking_interval=90,
        retain=7200,
    )
    mon.stats_limit_pol(
        description="Statistics instance limit for the policy.",
        instance_limit=600,
        include_action="all",
    )

    cfg.push(live_aci)


def test_tenant_monitoring_source_matrix(live_aci: Niwaki) -> None:
    """Severity x include-action cross, factored onto its own policy.

    A source carries one severity and one include-action flag set, so their cross
    is factored into one source per (severity, include-action) pair.
    """
    cfg = design()
    mon = cfg.tenant(TN, description=TN_DESC).monitoring_policy(
        MON_MATRIX, description="Tenant monitoring policy: the severity x include-action cross."
    )
    for si, sev in enumerate(SYSLOG_SEVERITIES):
        for ai, incl in enumerate(INCLUDE_ACTIONS):
            mon.syslog_source(
                f"sl-{si}-{ai}",
                description=f"Syslog source, severity {sev}, include {incl}.",
                min_severity=sev,
                include_action=incl,
            )
    cfg.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (f"uni/tn-{TN}",):  # a tenant delete cascades the monitoring policies
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
