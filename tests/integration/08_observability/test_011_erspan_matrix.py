"""Observability — ERSPAN destination matrix, factored coverage (non-prod).

Run:
    uv run pytest tests/integration/08_observability/test_011_erspan_matrix.py -m integration -s

A SPAN destination group holds a single destination, and an ERSPAN summary carries
one mode, one DSCP mark and one TTL — so the mode/DSCP/TTL value space cannot be
swept on a single object. This file factors it out: one fabric SPAN destination
group per combination, so every DSCP mark is exercised under each visibility mode,
and the TTL range is swept on its own. Every summary is a full ERSPAN destination
with a collector IP.

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
#   maker. The EPG SPAN-destination bind is rejected live (an ERSPAN IP is always required).

PREFIX = "niwaki-it-em"

# Every DSCP mark the ERSPAN summary accepts (the APIC renames the numbers on the wire).
DSCP_VALUES: tuple[int | str, ...] = (
    0,
    8,
    10,
    12,
    14,
    16,
    18,
    20,
    22,
    24,
    26,
    28,
    30,
    32,
    34,
    36,
    38,
    40,
    44,
    46,
    48,
    56,
    64,
    "unspecified",
)
MODES = ("visible", "not-visible")
# TTL is a named number: 0 is stored as "unspecified".
TTL_VALUES: tuple[int | str, ...] = ("unspecified", 1, 16, 32, 64, 128, 255)
MTU_VALUES = (1518, 1500, 9216)


def _mode_tag(mode: str) -> str:
    """A DN-safe fragment for a visibility mode."""
    return "vis" if mode == "visible" else "nvis"


def test_erspan_dscp_mode_matrix(live_aci: Niwaki) -> None:
    """One ERSPAN destination group per (DSCP, mode) pair — the full cross."""
    fab = fabric()
    for di, dscp in enumerate(DSCP_VALUES):
        for mode in MODES:
            name = f"{PREFIX}-d{dscp}-{_mode_tag(mode)}"
            grp = fab.span_destination_group(name, description=f"ERSPAN dscp {dscp}, {mode}.")
            grp.span_destination("dst").vspan_epg_summary(
                description=f"ERSPAN summary: dscp {dscp}, mode {mode}.",
                destination_ip=f"10.96.{di}.1",
                source_ip_of_erspan_packet=f"10.96.{di}.254",
                flow_id=(di % 250) + 1,
                mode=mode,
                dscp=dscp,
                time_to_live=TTL_VALUES[di % len(TTL_VALUES)],
                mtu=MTU_VALUES[di % len(MTU_VALUES)],
            )
    fab.push(live_aci)


def test_erspan_ttl_sweep(live_aci: Niwaki) -> None:
    """One ERSPAN destination group per TTL value (fixed DSCP/mode)."""
    fab = fabric()
    for ti, ttl in enumerate(TTL_VALUES):
        name = f"{PREFIX}-ttl-{ttl}"
        grp = fab.span_destination_group(name, description=f"ERSPAN ttl {ttl}.")
        grp.span_destination("dst").vspan_epg_summary(
            description=f"ERSPAN summary: ttl {ttl}.",
            destination_ip=f"10.97.{ti}.1",
            source_ip_of_erspan_packet=f"10.97.{ti}.254",
            flow_id=ti + 1,
            mode="visible",
            dscp=46,
            time_to_live=ttl,
        )
    fab.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    dns: list[str] = []
    for dscp in DSCP_VALUES:
        for mode in MODES:
            dns.append(f"uni/fabric/destgrp-{PREFIX}-d{dscp}-{_mode_tag(mode)}")
    for ttl in TTL_VALUES:
        dns.append(f"uni/fabric/destgrp-{PREFIX}-ttl-{ttl}")
    for dn in dns:
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
