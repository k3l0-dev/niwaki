"""Observability — syslog groups and their sinks, combination coverage (non-prod).

Run:
    uv run pytest tests/integration/08_observability/test_009_syslog_groups.py -m integration -s

The operator builds a spread of syslog destination groups sweeping the format and
timestamp value space (timezone/milliseconds are only accepted with the ACI
format), each carrying a console sink, a file sink and a protocol profile so that
every console/file severity, every format and both admin states are exercised.

Exhaustive, non-prod. ``wipe(aci)`` (operator-only) removes the whole set.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import fabric
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

# COVERAGE GAPS: universal MO children (tag/annotation/rbac/domain-tag-ref) globally have no
#   maker. Timezone/milliseconds in the timestamp are only accepted with the ACI format.

# (suffix, group_format, timezone, millis, console_sev, console_format, console_admin,
#  file_sev, file_format, file_admin, profile_admin)
GROUPS = (
    (
        "aci-full",
        "aci",
        True,
        True,
        "alerts",
        "aci",
        "enabled",
        "alerts",
        "aci",
        "enabled",
        "enabled",
    ),
    (
        "aci-tz",
        "aci",
        True,
        False,
        "critical",
        "nxos",
        "disabled",
        "critical",
        "nxos",
        "disabled",
        "disabled",
    ),
    (
        "aci-ms",
        "aci",
        False,
        True,
        "emergencies",
        "rfc5424-ts",
        "enabled",
        "debugging",
        "rfc5424-ts",
        "enabled",
        "enabled",
    ),
    (
        "aci-plain",
        "aci",
        False,
        False,
        "alerts",
        "aci",
        "disabled",
        "emergencies",
        "aci",
        "disabled",
        "disabled",
    ),
    (
        "nxos",
        "nxos",
        False,
        False,
        "critical",
        "nxos",
        "enabled",
        "errors",
        "nxos",
        "enabled",
        "enabled",
    ),
    (
        "rfc5424",
        "rfc5424-ts",
        False,
        False,
        "emergencies",
        "rfc5424-ts",
        "disabled",
        "information",
        "rfc5424-ts",
        "disabled",
        "disabled",
    ),
    (
        "mix1",
        "aci",
        True,
        True,
        "alerts",
        "nxos",
        "enabled",
        "notifications",
        "aci",
        "enabled",
        "enabled",
    ),
    (
        "mix2",
        "nxos",
        False,
        False,
        "critical",
        "aci",
        "disabled",
        "warnings",
        "nxos",
        "disabled",
        "disabled",
    ),
)

GROUP_PREFIX = "niwaki-it-syslog"


def test_syslog_groups(live_aci: Niwaki) -> None:
    """A syslog group per format/timestamp combination, each with console/file/profile."""
    day2 = fabric()
    for spec in GROUPS:
        (
            suffix,
            group_format,
            timezone,
            millis,
            console_sev,
            console_format,
            console_admin,
            file_sev,
            file_format,
            file_admin,
            profile_admin,
        ) = spec
        group = day2.syslog_group(
            f"{GROUP_PREFIX}-{suffix}",
            description=f"Syslog group, {group_format} format.",
            format_setting=group_format,
            show_timezone_in_timestamp=timezone,
            show_milli_seconds_in_timestamp=millis,
        )
        group.console(
            description=f"Console sink, severity {console_sev}.",
            admin_state=console_admin,
            severity=console_sev,
            format_setting=console_format,
        )
        group.file(
            description=f"File sink, severity {file_sev}.",
            admin_state=file_admin,
            severity=file_sev,
            format_setting=file_format,
        )
        group.protocol_profile(
            description="Syslog protocol profile.",
            admin_state=profile_admin,
        )

    day2.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for spec in GROUPS:
        with contextlib.suppress(NotFoundError):
            aci.node(f"uni/fabric/slgroup-{GROUP_PREFIX}-{spec[0]}").delete()
