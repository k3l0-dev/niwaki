"""Observability — tenant SPAN, combination coverage (non-prod).

Run:
    uv run pytest tests/integration/08_observability/test_005_span_tenant.py -m integration -s

The operator builds tenant SPAN: several application EPGs, a source group whose
sources span each EPG in each direction (EPG is the tenant-SPAN source relation the
APIC allows), ERSPAN destination groups, and a virtual-SPAN session and destination
group. Tenant SPAN destinations are ERSPAN (they carry a destination IP).

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
#   spanVSrc / spanVSrcDef carry no target binds (created with attributes only).
#   Universal MO children (tag/annotation/rbac/domain-tag-ref) globally have no maker.
# APIC scope rules honoured: only the EPG SPAN *source* relation is valid for tenant SPAN
#   (BD/VRF are fabric-only, L3Out is infra-only, node span is fabric-only). The EPG SPAN
#   *destination* bind (spanRsDestEpg) is rejected live ("Destination IP must be specified",
#   even alongside an ERSPAN summary), so SPAN/VSPAN destinations here are ERSPAN.

TN = "niwaki-it-obs-span-tn"
FILTER_GROUP = "niwaki-it-fg"
SRC = "niwaki-it-tenant-span"
DST = "niwaki-it-tenant-dst"
VSRC = "niwaki-it-tenant-vspan"
VDST = "niwaki-it-tenant-vdest"

# The tenant SPAN source surface is factored across several tenants.
SRC_TENANTS = (TN, "niwaki-it-obs-span-tn2", "niwaki-it-obs-span-tn3")
EPGS = ("web", "app", "db")
DIRECTIONS = ("both", "in", "out")
# ERSPAN destination combinations: (mode, ttl, dscp).
ERSPAN_COMBOS = (
    ("visible", 64, 46),
    ("not-visible", 32, 10),
    ("visible", 255, 0),
)


def test_tenant_span_sources(live_aci: Niwaki) -> None:
    """A source group spanning each EPG in each direction, factored across tenants."""
    cfg = design()
    # Cross-domain target for the source-group bind (the access filter group).
    cfg.infra().filter_group(FILTER_GROUP, description="Access SPAN flow filter group.")

    for tn in SRC_TENANTS:
        tenant = cfg.tenant(tn, description="Observability: tenant SPAN and VSPAN.")
        tenant.vrf("vrf")
        tenant.bd("bd").bind(vrf="vrf")
        app = tenant.app("ap")
        for epg in EPGS:
            app.epg(epg).bind(bd="bd")

        src = tenant.span_source_group(
            SRC, description="Tenant SPAN source group.", administrative_state="enabled"
        )
        src.bind(filter_group=FILTER_GROUP)
        src.span_label(
            DST, description="Match label onto the tenant destination group.", tag="green"
        )
        for epg in EPGS:
            for direction in DIRECTIONS:
                src.span_source(
                    f"{epg}-{direction}",
                    description=f"Source scoped to EPG {epg}, {direction}.",
                    direction_ingress_egress_both=direction,
                ).bind(epg=epg)

    cfg.push(live_aci)


def test_tenant_span_destinations(live_aci: Niwaki) -> None:
    """One ERSPAN destination group per combo (a group holds a single destination)."""
    cfg = design()
    tenant = cfg.tenant(TN, description="Observability: tenant SPAN and VSPAN.")
    for i, (mode, ttl, dscp) in enumerate(ERSPAN_COMBOS):
        tenant.span_destination_group(
            f"{DST}-{i}", description=f"Tenant ERSPAN destination group {i}."
        ).span_destination(f"dst-{i}").vspan_epg_summary(
            description=f"ERSPAN summary: {mode}, ttl {ttl}, dscp {dscp}.",
            destination_ip=f"10.92.{i}.1",
            source_ip_of_erspan_packet=f"10.92.{i}.254",
            flow_id=i + 1,
            mode=mode,
            time_to_live=ttl,
            dscp=dscp,
        )
    cfg.push(live_aci)


def test_tenant_vspan(live_aci: Niwaki) -> None:
    """A virtual-SPAN session and destination group bound to EPGs."""
    cfg = design()
    tenant = cfg.tenant(TN, description="Observability: tenant SPAN and VSPAN.")
    tenant.vrf("vrf")
    tenant.bd("bd").bind(vrf="vrf")
    app = tenant.app("ap")
    for epg in EPGS:
        app.epg(epg).bind(bd="bd")

    session = tenant.vspan_session(
        VSRC, description="Tenant virtual-SPAN session.", admin_state="start"
    )
    for direction in DIRECTIONS:
        session.vspan_vsource(f"vsrc-{direction}", direction_ingress_egress_both=direction)
    session.span_label(VDST, description="Match label onto the tenant VSPAN destination.")

    # A VSPAN destination is ERSPAN (it carries a destination IP).
    vdst = tenant.vspan_destination_group(
        VDST, description="Tenant virtual-SPAN destination group."
    )
    for i, (mode, ttl, dscp) in enumerate(ERSPAN_COMBOS):
        vdst.vspan_vdestination(f"vdst-{i}").vspan_destination_epg_summary(
            description=f"VSPAN ERSPAN summary: {mode}, ttl {ttl}.",
            destination_ip=f"10.95.{i}.1",
            source_ip_of_erspan_packet=f"10.95.{i}.254",
            flow_id=i + 1,
            mode=mode,
            time_to_live=ttl,
            dscp=dscp,
        )

    cfg.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for tn in SRC_TENANTS:  # each cascades the tenant SPAN/VSPAN + VRF/BD/AP/EPGs
        with contextlib.suppress(NotFoundError):
            aci.node(f"uni/tn-{tn}").delete()
