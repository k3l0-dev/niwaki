"""Fabric — VSPAN sessions/destinations and TWAMP (exhaustive combinations, non-prod).

Run:
    uv run pytest tests/integration/03_fabric/test_016_vspan_twamp.py -m integration -s

One VSPAN session per admin state, each carrying a VSPAN vsource per direction
and a match label; VSPAN destination groups (a single ERSPAN-to-EPG vdestination
each) covering both ERSPAN versions and visibility modes; and the TWAMP responder
and server policies across their admin states and timers.

Exhaustive combination coverage, illustrative values — not a real fabric config.

``wipe(aci)`` (operator-only) removes the VSPAN sessions, VSPAN destination
groups and TWAMP policies.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import fabric, ref
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

INB_MGMT_EPG = "uni/tn-mgmt/mgmtp-default/inb-default"

SESSION_PREFIX = "niwaki-it-vspan"
VDEST_PREFIX = "niwaki-it-vspan-dst"
TWAMP_RESP = "niwaki-it-twamp-resp"
TWAMP_SRV = "niwaki-it-twamp-srv"

SESSION_STATES = ("start", "stop", "unknown")
DIRECTIONS = ("in", "out", "both")
# (version, mode) — one VSPAN destination group each.
VDEST_SPECS = (
    ("ver1", "visible"),
    ("ver2", "not-visible"),
    ("ver2", "visible"),
)


def test_vspan_sessions(live_aci: Niwaki) -> None:
    fab = fabric()
    for admin in SESSION_STATES:
        session = fab.vspan_session(
            f"{SESSION_PREFIX}-{admin}",
            description=f"VSPAN session admin-state sweep ({admin}).",
            admin_state=admin,
        )
        for direction in DIRECTIONS:
            session.vspan_vsource(
                f"vsrc-{direction}",
                description=f"VSPAN vsource, {direction}.",
                direction_ingress_egress_both=direction,
            )
        session.span_label(
            f"match-{admin}",
            description=f"VSPAN session match label, {admin}.",
            tag="teal" if admin == "start" else "olive",
        )
    fab.push(live_aci)


def test_vspan_destination_groups(live_aci: Niwaki) -> None:
    fab = fabric()
    for idx, (version, mode) in enumerate(VDEST_SPECS):
        group = fab.vspan_destination_group(
            f"{VDEST_PREFIX}-{idx}",
            description=f"VSPAN destination group {version}, {mode}.",
        )
        vdest = group.vspan_vdestination(
            "vdest",
            description=f"VSPAN ERSPAN vdestination {version}.",
        ).bind_dn(
            epg=ref(
                INB_MGMT_EPG,
                ip=f"192.0.2.{70 + idx}",
                src_ip_prefix="192.0.2.0/24",
                ver=version,
                mtu=1518,
                ttl=64,
            )
        )
        vdest.vspan_destination_epg_summary(
            description=f"VSPAN ERSPAN summary {version}, {mode}.",
            destination_ip=f"192.0.2.{70 + idx}",
            source_ip_of_erspan_packet="192.0.2.0/24",
            mode=mode,
            dscp="CS4",
            mtu=1518,
            time_to_live=64,
            flow_id=idx + 1,
        )
    fab.push(live_aci)


def test_twamp_policies(live_aci: Niwaki) -> None:
    fab = fabric()
    for admin, timeout in (("enabled", 900), ("disabled", 300)):
        fab.twamp_responder_policy(
            f"{TWAMP_RESP}-{admin}",
            description=f"TWAMP responder {admin}, {timeout} s timeout.",
            twamp_responder_enable_disable=admin,
            twamp_responder_timeout=timeout,
        )
    for admin, port, timer in (("enabled", 862, 900), ("disabled", 863, 300)):
        fab.twamp_server_policy(
            f"{TWAMP_SRV}-{admin}",
            description=f"TWAMP server {admin}, port {port}, {timer} s inactivity.",
            twamp_server_enable_disable=admin,
            twamp_server_port_number=port,
            twamp_server_inactivity_timer=timer,
        )
    fab.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    dns = [f"uni/fabric/vsrcgrp-{SESSION_PREFIX}-{a}" for a in SESSION_STATES]
    dns += [f"uni/fabric/vdestgrp-{VDEST_PREFIX}-{i}" for i in range(len(VDEST_SPECS))]
    dns += [f"uni/fabric/twampRespP-{TWAMP_RESP}-{a}" for a in ("enabled", "disabled")]
    dns += [f"uni/fabric/twampServP-{TWAMP_SRV}-{a}" for a in ("enabled", "disabled")]
    for dn in dns:
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
