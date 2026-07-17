"""Fabric — SNMP and TACACS monitoring destinations (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/03_fabric/test_007_snmp_tacacs.py -m integration -s

The SNMP monitoring destination group carries one trap destination per SNMP
version, and for v3 one per security level (noauth / auth / priv); every SNMP
trap destination is associated with the out-of-band management EPG. The TACACS
monitoring destination group carries one destination per
``(authentication-protocol, command-argument-logging)`` combination.

Exhaustive combination coverage, illustrative values — not a real fabric config.

``wipe(aci)`` (operator-only) removes both destination groups.

# COVERAGE GAPS (curated parent, child/relation not reachable via the DSL):
#   - tacacsTacacsDest: attachable_target_group (fileRsARemoteHostToEpg) is not
#     curated, so a TACACS destination cannot be bound to a management EPG.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import fabric
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

OOB_MGMT_EPG = "uni/tn-mgmt/mgmtp-default/oob-default"

SNMP = "niwaki-it-snmp"
TACACS = "niwaki-it-tacacs"

# (host, version, v3 security level or None)
SNMP_DESTS = (
    ("192.0.2.11", "v1", None),
    ("192.0.2.12", "v2c", None),
    ("192.0.2.13", "v3", "noauth"),
    ("192.0.2.14", "v3", "auth"),
    ("192.0.2.15", "v3", "priv"),
)
TACACS_AUTH = ("chap", "mschap", "pap")


def test_snmp_destination_group(live_aci: Niwaki) -> None:
    fab = fabric()
    group = fab.snmp_monitoring_destination_group(
        SNMP,
        description="SNMP trap destination group across versions and v3 levels.",
    )
    for host, version, sec_level in SNMP_DESTS:
        dest = group.snmp_trap_destination(
            host,
            162,
            description=f"SNMP {version} trap destination"
            + (f", v3 {sec_level}." if sec_level else "."),
            version=version,
            notif_t="traps",
            security_name="niwaki-community",
            v3_security_level=sec_level if sec_level else None,
        )
        dest.bind_dn(management_epg=OOB_MGMT_EPG)
    fab.push(live_aci)


def test_tacacs_destination_group(live_aci: Niwaki) -> None:
    fab = fabric()
    group = fab.tacacs_monitoring_destination_group(
        TACACS,
        description="TACACS+ destination group across auth protocols and logging.",
    )
    port = 49
    for proto in TACACS_AUTH:
        for log_args in (True, False):
            group.tacacs_destination(
                f"192.0.2.{port}",
                port,
                description=f"TACACS+ destination, {proto}, command args {log_args}.",
                authentication_protocol=proto,
                key="niwaki-tacacs-secret",
                send_changes_as_command_arguments=log_args,
            )
            port += 1
    fab.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (
        f"uni/fabric/snmpgroup-{SNMP}",
        f"uni/fabric/tacacsgroup-{TACACS}",
    ):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
