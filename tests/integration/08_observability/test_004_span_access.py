"""Observability — access (infra) SPAN, combination coverage (non-prod).

Run:
    uv run pytest tests/integration/08_observability/test_004_span_access.py -m integration -s

The operator builds access SPAN: a filter group with one entry per IP protocol, a
source group whose sources span an interface path (every direction) and an L3Out
(infra-SPAN only), the virtual-source makers, and a destination group whose ERSPAN
summaries sweep the visible/not-visible mode, TTL, DSCP and MTU value space. A
virtual-SPAN session and destination group round it out.

Exhaustive, non-prod. Paths are data-driven from the live fabric. ``wipe(aci)``
(operator-only) removes the whole set.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import design
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

# COVERAGE GAPS (curated child in CHILD_MAP, no maker/bind/verb — reported, not forced):
#   spanFilterEntry: extended_filter_entry (spanExtendedFltEntry) — no maker.
#   spanVSrc / spanVSrcDef: no target binds are curated — created with attributes only.
#   Universal MO children (tag/annotation/rbac/domain-tag-ref) globally have no maker.
# APIC scope rules honoured: L3Out relation is infra-SPAN only; span-on-drop is not accepted
#   under access SPAN; the apic_node destination is rejected live (ERSPAN used instead).

TN = "niwaki-it-obs-span-acc"
FILTER_GROUP = "niwaki-it-fg"
SRC = "niwaki-it-access-span"
DST = "niwaki-it-access-dst"
VSRC = "niwaki-it-access-vspan"
VDST = "niwaki-it-access-vdest"

# One filter entry per IP protocol (named numbers the APIC renames on the wire).
PROTOCOLS = ("tcp", "udp", "icmp", "igmp", "pim", "ospfigp", "eigrp", "l2tp")
DIRECTIONS = ("both", "in", "out")
# ERSPAN summary combinations: (mode, ttl, dscp, mtu).
ERSPAN_COMBOS = (
    ("visible", 64, 0, 1518),
    ("not-visible", "unspecified", 10, 1500),
    ("visible", 255, 46, 9216),
    ("not-visible", 32, 8, 1500),
    ("visible", 128, 34, 1518),
    ("not-visible", 16, 24, 9216),
)


def _path_dn(live_aci: Niwaki) -> str:
    """Return an access interface path DN for the first leaf the fabric reports."""
    for node in live_aci.query("fabricNode").fetch():
        data = node.model_dump(by_alias=True)
        if data.get("role") == "leaf" and data.get("id"):
            return f"topology/pod-1/paths-{data['id']}/pathep-[eth1/1]"
    return "topology/pod-1/paths-101/pathep-[eth1/1]"


def test_access_span_filter_group(live_aci: Niwaki) -> None:
    """A filter group with one flow-filter entry per IP protocol."""
    cfg = design()
    fg = cfg.infra().filter_group(FILTER_GROUP, description="Access SPAN flow filter group.")
    for i, proto in enumerate(PROTOCOLS):
        fg.filter_entry(
            proto,
            f"10.{i}.0.0/24",
            f"10.{i}.1.0/24",
            0,
            0,
            0,
            0,
            description=f"Mirror {proto} flows.",
        )
    # A couple of entries carrying explicit L4 port ranges as well.
    fg.filter_entry("tcp", "10.20.0.0/24", "10.20.1.0/24", 0, 0, 80, 80, description="HTTP flows.")
    fg.filter_entry(
        "tcp", "10.21.0.0/24", "10.21.1.0/24", 1024, 2048, 443, 443, description="HTTPS flows."
    )
    cfg.push(live_aci)


def test_access_span_sources(live_aci: Niwaki) -> None:
    """A source group spanning an interface path (every direction) and an L3Out."""
    path_dn = _path_dn(live_aci)

    cfg = design()
    infra = cfg.infra()
    # Cross-domain target for the source-group bind (declared with entries elsewhere).
    infra.filter_group(FILTER_GROUP, description="Access SPAN flow filter group.")

    # An L3Out for an access source to span (the L3Out relation is infra-SPAN only).
    tenant = cfg.tenant(TN, description="Observability: access SPAN L3Out target.")
    tenant.vrf("vrf")
    tenant.l3out("lo").bind(vrf="vrf")

    src = infra.span_source_group(
        SRC, description="Access SPAN source group.", administrative_state="enabled"
    )
    src.bind(filter_group=FILTER_GROUP)
    src.span_label(DST, description="Match label onto the destination group.", tag="blue")

    for direction in DIRECTIONS:
        src.span_source(
            f"path-{direction}",
            description=f"Access source spanning a path, {direction}.",
            direction_ingress_egress_both=direction,
        ).bind_dn(path=path_dn)
    src.span_source(
        "l3out-src",
        description="Access source scoped to an L3Out.",
        direction_ingress_egress_both="both",
    ).bind(l3out="lo")

    # The virtual-source makers under a SPAN source group, over every direction.
    for direction in DIRECTIONS:
        src.vspan_source(f"vsrc-{direction}", direction_ingress_egress_both=direction)
    src.vspan_source_def("vsrcdef", direction_ingress_egress_both="both")

    cfg.push(live_aci)


def test_access_span_destinations(live_aci: Niwaki) -> None:
    """One destination group per ERSPAN combo (a group holds a single destination)."""
    cfg = design()
    infra = cfg.infra()
    for i, (mode, ttl, dscp, mtu) in enumerate(ERSPAN_COMBOS):
        grp = infra.span_destination_group(f"{DST}-{i}", description=f"Access SPAN dest group {i}.")
        grp.span_destination(f"dst-{i}").vspan_epg_summary(
            description=f"ERSPAN summary: {mode}, ttl {ttl}, dscp {dscp}, mtu {mtu}.",
            destination_ip=f"10.90.{i}.1",
            source_ip_of_erspan_packet=f"10.90.{i}.254",
            flow_id=i + 1,
            mode=mode,
            time_to_live=ttl,
            dscp=dscp,
            mtu=mtu,
        )
    cfg.push(live_aci)


def test_access_vspan(live_aci: Niwaki) -> None:
    """A virtual-SPAN session and destination group with ERSPAN summaries."""
    cfg = design()
    infra = cfg.infra()

    session = infra.vspan_session(
        VSRC, description="Access virtual-SPAN session.", admin_state="start"
    )
    for direction in DIRECTIONS:
        session.vspan_vsource(f"vsrc-{direction}", direction_ingress_egress_both=direction)

    vdst = infra.vspan_destination_group(VDST, description="Access virtual-SPAN destination group.")
    for i, (mode, ttl, dscp, mtu) in enumerate(ERSPAN_COMBOS[:3]):
        vdst.vspan_vdestination(f"vdst-{i}").vspan_destination_epg_summary(
            description=f"VSPAN ERSPAN summary: {mode}, ttl {ttl}.",
            destination_ip=f"10.91.{i}.1",
            source_ip_of_erspan_packet=f"10.91.{i}.254",
            flow_id=i + 1,
            mode=mode,
            time_to_live=ttl,
            dscp=dscp,
            mtu=mtu,
        )
    cfg.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    dns = [
        f"uni/tn-{TN}",
        f"uni/infra/filtergrp-{FILTER_GROUP}",
        f"uni/infra/srcgrp-{SRC}",
        f"uni/infra/vsrcgrp-{VSRC}",
        f"uni/infra/vdestgrp-{VDST}",
    ]
    dns += [f"uni/infra/destgrp-{DST}-{i}" for i in range(len(ERSPAN_COMBOS))]
    for dn in dns:
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
