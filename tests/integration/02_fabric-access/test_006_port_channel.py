"""Fabric access — port-channel (LACP) interface policies (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/02_fabric-access/test_006_port_channel.py -m integration -s

The port-channel shelf: the LACP bundle policy (one per LACP mode, plus a sweep of
the control-flag combinations and min/max member counts, each carrying a
load-balance child with a distinct hash-field set), the LACP member policy (both
transmit rates), and the link-flap policy. Values are illustrative and cover the
SDK surface, not a real bundle plan.

# COVERAGE GAPS (curated child in CHILD_MAP but reachable only via .mo(), and it
# marks the parent extMngdBy=msc — deliberately not configured):
#   - external_tag_instance (tagExtMngdInst) / tag_instance (tagInst) on
#     lacpLagPol, lacpIfPol, fabricLinkFlapPol

This file owns only its niwaki-it-* policies; wipe(aci) removes them and is run by
hand (never by the suite).
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import Cursor, infra
from niwaki.exceptions import NotFoundError
from niwaki.models._generated.aaa.aaaRbacAnnotation import aaaRbacAnnotation
from niwaki.models._generated.tag.tagAnnotation import tagAnnotation
from niwaki.models._generated.tag.tagTag import tagTag

pytestmark = pytest.mark.integration

LACP_MODES = (
    "active",
    "explicit-failover",
    "mac-pin",
    "mac-pin-nicload",
    "off",
    "passive",
)
# Load-balance hash-field combinations (PcLbL3L4 flags), cycled across the modes.
LB_CRITERIA = (
    "src-ip",
    "dst-ip",
    "l4-src-port",
    "l4-dst-port",
    "src-ip,dst-ip",
    "src-ip,dst-ip,l4-src-port,l4-dst-port",
)
# Control-flag combinations (PcIfControl): empty, each flag alone, multi, all.
# The ``load-defer`` flag is rejected by the 6.0(9c) simulator ("not a supported
# option"), so it is left out of the combinations.
CTRL_COMBOS: tuple[tuple[str, str], ...] = (
    ("none", ""),
    ("susp", "susp-individual"),
    ("graceful", "graceful-conv"),
    ("faststdby", "fast-sel-hot-stdby"),
    ("symhash", "symmetric-hash"),
    ("multi", "susp-individual,graceful-conv,fast-sel-hot-stdby"),
    ("all", "susp-individual,graceful-conv,fast-sel-hot-stdby,symmetric-hash"),
)
TX_RATES = ("fast", "normal")


def _common(obj: Cursor) -> None:
    """Attach the universal children (annotation, tag, RBAC domain marker)."""
    obj.mo(tagAnnotation, key="orchestrator", value="niwaki-it")
    obj.mo(tagTag, key="lifecycle", value="integration")
    obj.mo(aaaRbacAnnotation, domain="all")


def _mode_name(mode: str) -> str:
    return f"niwaki-it-lacp-mode-{mode}"


def _ctrl_name(slug: str) -> str:
    return f"niwaki-it-lacp-ctrl-{slug}"


def _member_name(rate: str) -> str:
    return f"niwaki-it-lacpmbr-{rate}"


LINK_FLAP_NAMES = ("niwaki-it-linkflap-fast", "niwaki-it-linkflap-slow")


def test_lacp_modes(live_aci: Niwaki) -> None:
    """One LACP bundle policy per LACP mode, each with a load-balance child."""
    fab = infra()
    for idx, mode in enumerate(LACP_MODES):
        lag = fab.lacp_policy(
            _mode_name(mode),
            mode=mode,
            maximum_number_of_links=16,
            minimum_number_of_links=1,
            description=f"LACP mode sweep - {mode}.",
        )
        _common(lag)
        criteria = LB_CRITERIA[idx % len(LB_CRITERIA)]
        lb = lag.load_balance_policy(
            load_balance_criteria=criteria,
            description=f"Port-channel load balancing on {criteria}.",
        )
        _common(lb)
    fab.push(live_aci)


def test_lacp_controls(live_aci: Niwaki) -> None:
    """LACP bundle policy across the control-flag combinations + link counts."""
    fab = infra()
    for idx, (slug, ctrl) in enumerate(CTRL_COMBOS):
        max_links = (16, 8, 4)[idx % 3]
        min_links = (1, 2, 1)[idx % 3]
        lag = fab.lacp_policy(
            _ctrl_name(slug),
            mode="active",
            control=ctrl or None,
            maximum_number_of_links=max_links,
            minimum_number_of_links=min_links,
            description=f"LACP control-flag sweep - ({slug}), min {min_links}, max {max_links}.",
        )
        _common(lag)
    fab.push(live_aci)


def test_lacp_member(live_aci: Niwaki) -> None:
    """LACP member policy across both transmit rates."""
    fab = infra()
    for rate in TX_RATES:
        member = fab.lacp_member_policy(
            _member_name(rate),
            transmission_rate=rate,
            priority=32768 if rate == "normal" else 100,
            description=f"LACP member transmit-rate sweep - {rate}.",
        )
        _common(member)
    fab.push(live_aci)


def test_link_flap(live_aci: Niwaki) -> None:
    """Link-flap policy across two flap thresholds."""
    fab = infra()
    fast = fab.link_flap_policy(
        LINK_FLAP_NAMES[0],
        max_flaps_allowed_per_time=5,
        time_allowed_for_max_flaps=30,
        description="Link-flap threshold - 5 flaps in 30s.",
    )
    _common(fast)
    slow = fab.link_flap_policy(
        LINK_FLAP_NAMES[1],
        max_flaps_allowed_per_time=30,
        time_allowed_for_max_flaps=420,
        description="Link-flap threshold - 30 flaps in 420s.",
    )
    _common(slow)
    fab.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    names: list[str] = []
    names += [_mode_name(m) for m in LACP_MODES]
    names += [_ctrl_name(slug) for slug, _ in CTRL_COMBOS]
    lag_dns = [f"uni/infra/lacplagp-{n}" for n in names]
    member_dns = [f"uni/infra/lacpifp-{_member_name(r)}" for r in TX_RATES]
    flap_dns = [f"uni/infra/linkflappol-{n}" for n in LINK_FLAP_NAMES]
    for dn in (*lag_dns, *member_dns, *flap_dns):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
