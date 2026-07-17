"""Observability — syslog remote destinations, combination coverage (non-prod).

Run:
    uv run pytest tests/integration/08_observability/test_010_syslog_remotes.py -m integration -s

The operator points the fabric at a fleet of remote syslog collectors, sweeping
the transport, forwarding-facility, severity, format and admin-state value space —
every forwarding facility (local0..local7) and every syslog severity is covered,
with the transports and formats rotated across the set. Every collector is
reachable over the OOB management EPG.

Exhaustive, non-prod. ``wipe(aci)`` (operator-only) removes the whole set.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import fabric
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

# COVERAGE GAPS: syslogRemoteDest.relation_to_remote_host_reachability_epp (fileRsARemoteHostToEpp)
#   is uncurated — only the management_epg bind (fileRsARemoteHostToEpg) is reachable.
#   Universal MO children (tag/annotation/rbac/domain-tag-ref) globally have no maker.

GROUP = "niwaki-it-syslog-remotes"
GROUP_FS = "niwaki-it-syslog-remotes-fs"
OOB_MGMT_EPG = "uni/tn-mgmt/mgmtp-default/oob-default"  # the fabric's default OOB EPG

FACILITIES = ("local0", "local1", "local2", "local3", "local4", "local5", "local6", "local7")
SEVERITIES = (
    "alerts",
    "critical",
    "debugging",
    "emergencies",
    "errors",
    "information",
    "notifications",
    "warnings",
)
# (protocol, default port).
TRANSPORTS = (("udp", 514), ("tcp", 601), ("ssl", 6514))
FORMATS = ("aci", "nxos", "rfc5424-ts")


def test_syslog_remote_format_transport(live_aci: Niwaki) -> None:
    """The format x transport cross, factored into one collector per pair.

    A remote destination carries one format and one transport, so their cross is
    factored into one destination per (format, transport) pair.
    """
    day2 = fabric()
    group = day2.syslog_group(
        GROUP,
        description="Syslog group: the format x transport cross.",
        format_setting="aci",
    )
    idx = 0
    for fmt in FORMATS:
        for protocol, port in TRANSPORTS:
            group.remote_destination(
                f"10.41.0.{idx + 1}",
                name=f"ft-{fmt}-{protocol}",
                description=f"Collector: {fmt} over {protocol}.",
                admin_state="enabled" if idx % 2 else "disabled",
                severity=SEVERITIES[idx % len(SEVERITIES)],
                protocol=protocol,
                port=port,
                forward_facility=FACILITIES[idx % len(FACILITIES)],
                format_setting=fmt,
            ).bind_dn(management_epg=OOB_MGMT_EPG)
            idx += 1
    day2.push(live_aci)


def test_syslog_remote_facility_severity(live_aci: Niwaki) -> None:
    """Every forwarding facility and every syslog severity, on their own collectors."""
    day2 = fabric()
    group = day2.syslog_group(
        GROUP_FS,
        description="Syslog group: every facility and every severity.",
        format_setting="aci",
    )
    for i in range(len(FACILITIES)):
        protocol, port = TRANSPORTS[i % len(TRANSPORTS)]
        group.remote_destination(
            f"10.40.0.{i + 1}",
            name=f"collector-{i}",
            description=f"Collector: {protocol}, {FACILITIES[i]}, {SEVERITIES[i]}.",
            admin_state="enabled" if i % 2 else "disabled",
            severity=SEVERITIES[i],
            protocol=protocol,
            port=port,
            forward_facility=FACILITIES[i],
            format_setting=FORMATS[i % len(FORMATS)],
        ).bind_dn(management_epg=OOB_MGMT_EPG)
    day2.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (
        f"uni/fabric/slgroup-{GROUP}",
        f"uni/fabric/slgroup-{GROUP_FS}",
    ):  # cascades all remote destinations
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
