"""Fabric — callhome and smart-callhome destination groups (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/03_fabric/test_006_callhome.py -m integration -s

The callhome destination group carries one destination per urgency level (all
eight are exercised), cycling the message format across AML / short-text / XML
and toggling the admin state and RFC-compliance flags; the smart-callhome group
does the same across its formats. Each group carries a protocol (SMTP) profile,
and the two profiles between them cover both admin states and both secure-SMTP
settings.

Exhaustive combination coverage, illustrative values — not a real fabric config.

``wipe(aci)`` (operator-only) removes both destination groups.

# COVERAGE GAPS (curated parent, child/relation not reachable via the DSL):
#   - callhomeProf: smtp_server_for_callhome (callhomeSmtpServer) is not curated,
#     so the callhome SMTP relay server cannot be declared.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import fabric
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

CALLHOME = "niwaki-it-callhome"
SMART = "niwaki-it-smart-callhome"

URGENCIES = ("alert", "critical", "debug", "emergency", "error", "info", "notice", "warning")
FORMATS = ("aml", "short-txt", "xml")


def test_callhome_destination_group(live_aci: Niwaki) -> None:
    fab = fabric()
    group = fab.callhome_destination_group(
        CALLHOME,
        description="Callhome destination group covering every urgency level.",
    )
    for idx, urgency in enumerate(URGENCIES):
        group.callhome_destination(
            urgency,
            description=f"Callhome destination at {urgency} urgency.",
            admin_state="enabled" if idx % 2 == 0 else "disabled",
            destination_email_address=f"{urgency}@niwaki.example",
            message_format=FORMATS[idx % len(FORMATS)],
            message_format_rfc_compliant=idx % 2 == 0,
            maximum_size=1000000,
        )
    # Protocol profile: admin enabled, plain SMTP.
    group.callhome_protocol_profile(
        description="Callhome SMTP protocol profile, admin enabled, plain SMTP.",
        admin_state="enabled",
        contact_name="Fabric NOC",
        contact_email="noc@niwaki.example",
        from_="apic@niwaki.example",
        port=25,
        secure_smtp=False,
    )
    fab.push(live_aci)


def test_smart_callhome_destination_group(live_aci: Niwaki) -> None:
    fab = fabric()
    group = fab.smart_callhome_destination_group(
        SMART,
        description="Smart callhome destination group across every message format.",
    )
    for idx, fmt in enumerate(FORMATS):
        group.smart_callhome_destination(
            fmt,
            description=f"Smart callhome destination, {fmt} format.",
            admin_state="enabled" if idx % 2 == 0 else "disabled",
            destination_email_address=f"smart-{fmt}@niwaki.example",
            message_format=fmt,
            message_format_rfc_compliant=idx % 2 == 1,
        )
    # Protocol profile: admin disabled, secure SMTP — the complementary settings.
    group.callhome_protocol_profile(
        description="Smart callhome SMTP protocol profile, admin disabled, secure SMTP.",
        admin_state="disabled",
        contact_name="Cisco TAC",
        from_="apic@niwaki.example",
        port=465,
        secure_smtp=True,
    )
    fab.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (
        f"uni/fabric/chgroup-{CALLHOME}",
        f"uni/fabric/smartgroup-{SMART}",
    ):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
