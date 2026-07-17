"""Fabric — syslog monitoring destination groups (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/03_fabric/test_008_syslog.py -m integration -s

One syslog destination group per message format (ACI / NX-OS / RFC-5424); the
ACI-format group additionally enables the millisecond and timezone timestamp
flags (only accepted with the ACI format). Each group carries remote
destinations covering every severity and forwarding facility, cycling the
transport protocol and admin state (each remote destination bound to the
out-of-band management EPG), plus a console sink, a file sink and a protocol
profile whose severities and admin states are cycled across the groups.

Exhaustive combination coverage, illustrative values — not a real fabric config.

``wipe(aci)`` (operator-only) removes every syslog destination group.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import fabric
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

OOB_MGMT_EPG = "uni/tn-mgmt/mgmtp-default/oob-default"

PREFIX = "niwaki-it-syslog"
FORMATS = ("aci", "nxos", "rfc5424-ts")
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
FACILITIES = ("local0", "local1", "local2", "local3", "local4", "local5", "local6", "local7")
PROTOCOLS = ("ssl", "tcp", "udp")
CONSOLE_SEVERITIES = ("alerts", "critical", "emergencies")


def test_syslog_destination_groups(live_aci: Niwaki) -> None:
    fab = fabric()
    for fmt_idx, fmt in enumerate(FORMATS):
        is_aci = fmt == "aci"
        group = fab.syslog_group(
            f"{PREFIX}-{fmt}",
            description=f"Syslog severity/facility/transport matrix ({fmt} format).",
            format_setting=fmt,
            show_milli_seconds_in_timestamp=is_aci,
            show_timezone_in_timestamp=is_aci,
        )
        # One remote destination per severity, cycling facility / protocol / admin.
        # The enhanced RFC-5424 format only accepts the UDP transport.
        for sev_idx, severity in enumerate(SEVERITIES):
            protocol = "udp" if fmt == "rfc5424-ts" else PROTOCOLS[sev_idx % len(PROTOCOLS)]
            group.remote_destination(
                f"192.0.{fmt_idx + 1}.{sev_idx + 1}",
                description=f"Remote syslog collector, {severity}.",
                admin_state="enabled" if sev_idx % 2 == 0 else "disabled",
                severity=severity,
                forward_facility=FACILITIES[sev_idx % len(FACILITIES)],
                protocol=protocol,
                format_setting=fmt,
                port=514,
            ).bind_dn(management_epg=OOB_MGMT_EPG)
        group.console(
            description="Console syslog sink.",
            admin_state="enabled" if fmt_idx % 2 == 0 else "disabled",
            severity=CONSOLE_SEVERITIES[fmt_idx % len(CONSOLE_SEVERITIES)],
            format_setting=fmt,
        )
        group.file(
            description="Local file syslog sink.",
            admin_state="enabled" if fmt_idx % 2 == 1 else "disabled",
            severity=SEVERITIES[fmt_idx % len(SEVERITIES)],
            format_setting=fmt,
        )
        group.protocol_profile(
            description="Syslog protocol profile.",
            admin_state="enabled" if fmt_idx % 2 == 0 else "disabled",
        )
    fab.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for fmt in FORMATS:
        with contextlib.suppress(NotFoundError):
            aci.node(f"uni/fabric/slgroup-{PREFIX}-{fmt}").delete()
