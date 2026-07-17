"""Tenant contracts — QoS requirements and data-plane policing (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/05_contracts/test_011_qos.py -m integration -s

The QoS surface driven to the corners: ``qosDppPol`` policers across the cartesian of
policer type (1R2C / 2R3C) x metering mode (bit / packet) x sharing (dedicated /
shared), every rate and burst unit, and every conform / exceed / violate action
(including the mark actions with their DSCP/CoS mark fields). Then ``qosRequirement``
objects that mark DSCP and wire ingress/egress policers through the verbs.

Values are illustrative — this proves the SDK expresses the QoS surface, not a
production policing plan. ``wipe(aci)`` (operator-only) removes what this file owns.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

TN = "niwaki-it-contracts"
TN_DESC = "Exhaustive contract, filter, label, taboo, vzAny and QoS surface"

RATE_UNITS = ("giga", "kilo", "mega", "unspecified")
BURST_UNITS = ("giga", "kilo", "mega", "msec", "unspecified", "usec")
ACTIONS = ("drop", "mark", "transmit")

# Deterministic DPP names for wipe().
DPP_CART: list[str] = [
    f"niwaki-it-dpp-{t}-{m}-{s}"
    for t in ("1r2c", "2r3c")
    for m in ("bit", "packet")
    for s in ("ded", "shr")
]
DPP_RATE_UNITS = [f"niwaki-it-dpp-rate-{u}" for u in RATE_UNITS]
DPP_BURST_UNITS = [f"niwaki-it-dpp-burst-{u}" for u in BURST_UNITS]
DPP_ACTIONS = [
    f"niwaki-it-dpp-{role}-{a}" for role in ("conform", "exceed", "violate") for a in ACTIONS
]

QOS_REQS = ("niwaki-it-qos-a", "niwaki-it-qos-b")
QOS_DPP_IN = "niwaki-it-qos-dpp-in"
QOS_DPP_OUT = "niwaki-it-qos-dpp-out"


def test_dpp_type_mode_sharing(live_aci: Niwaki) -> None:
    # policer-type x metering-mode x sharing-mode. Bit mode carries rate/burst units;
    # packet mode meters in packets, so the byte-oriented units are left off.
    cfg = tenant(TN, description=TN_DESC)
    for pol_type, tt in (("1R2C", "1r2c"), ("2R3C", "2r3c")):
        for mode, mm in (("bit", "bit"), ("packet", "packet")):
            for sharing, ss in (("dedicated", "ded"), ("shared", "shr")):
                name = f"niwaki-it-dpp-{tt}-{mm}-{ss}"
                if pol_type == "1R2C":
                    cfg.dpp_policy(
                        name,
                        admin_st="enabled",
                        type=pol_type,
                        bit_or_packet=mode,
                        rate=1_000_000,
                        rate_unit="kilo" if mode == "bit" else "unspecified",
                        burst=200,
                        burst_unit="kilo" if mode == "bit" else "unspecified",
                        confirm_action="transmit",
                        exceed_action="drop",
                        policer_sharing_mode=sharing,
                        description=f"1R2C policer {mode}/{sharing}.",
                    )
                else:
                    cfg.dpp_policy(
                        name,
                        admin_st="enabled",
                        type=pol_type,
                        bit_or_packet=mode,
                        rate=1_000_000,
                        rate_unit="kilo" if mode == "bit" else "unspecified",
                        peak_rate=2_000_000,
                        peak_rate_unit="kilo" if mode == "bit" else "unspecified",
                        burst=200,
                        burst_unit="kilo" if mode == "bit" else "unspecified",
                        excessive_burst=400,
                        excessive_burst_unit="kilo" if mode == "bit" else "unspecified",
                        confirm_action="transmit",
                        exceed_action="transmit",
                        violate_action="drop",
                        policer_sharing_mode=sharing,
                        description=f"2R3C policer {mode}/{sharing}.",
                    )
    cfg.push(live_aci)


def test_dpp_units(live_aci: Niwaki) -> None:
    # Every rate unit and every burst unit (bit-metered 1R2C policers).
    cfg = tenant(TN, description=TN_DESC)
    for unit in RATE_UNITS:
        cfg.dpp_policy(
            f"niwaki-it-dpp-rate-{unit}",
            admin_st="disabled",
            type="1R2C",
            bit_or_packet="bit",
            rate=500_000,
            rate_unit=unit,
            burst=100,
            burst_unit="kilo",
            confirm_action="transmit",
            exceed_action="drop",
            description=f"Rate unit {unit}.",
        )
    for unit in BURST_UNITS:
        cfg.dpp_policy(
            f"niwaki-it-dpp-burst-{unit}",
            admin_st="enabled",
            type="1R2C",
            bit_or_packet="bit",
            rate=500_000,
            rate_unit="kilo",
            burst=100,
            burst_unit=unit,
            confirm_action="transmit",
            exceed_action="drop",
            description=f"Burst unit {unit}.",
        )
    cfg.push(live_aci)


def test_dpp_actions(live_aci: Niwaki) -> None:
    # Every conform / exceed / violate action, with mark fields where marking (the
    # DSCP/CoS mark fields are pruned when the action is not "mark").
    cfg = tenant(TN, description=TN_DESC)
    for action in ACTIONS:
        mark = action == "mark"
        cfg.dpp_policy(
            f"niwaki-it-dpp-conform-{action}",
            type="1R2C",
            bit_or_packet="bit",
            rate=500_000,
            rate_unit="kilo",
            burst=100,
            burst_unit="kilo",
            confirm_action=action,
            conform_mark_cos=3 if mark else None,
            conform_mark_dscp=10 if mark else None,
            exceed_action="drop",
            description=f"Conform action {action}.",
        )
    for action in ACTIONS:
        mark = action == "mark"
        cfg.dpp_policy(
            f"niwaki-it-dpp-exceed-{action}",
            type="1R2C",
            bit_or_packet="bit",
            rate=500_000,
            rate_unit="kilo",
            burst=100,
            burst_unit="kilo",
            confirm_action="transmit",
            exceed_action=action,
            exceed_mark_cos=3 if mark else None,
            exceed_mark_dscp=10 if mark else None,
            description=f"Exceed action {action}.",
        )
    for action in ACTIONS:
        mark = action == "mark"
        cfg.dpp_policy(
            f"niwaki-it-dpp-violate-{action}",
            type="2R3C",
            bit_or_packet="bit",
            rate=500_000,
            rate_unit="kilo",
            peak_rate=1_000_000,
            peak_rate_unit="kilo",
            burst=100,
            burst_unit="kilo",
            excessive_burst=200,
            excessive_burst_unit="kilo",
            confirm_action="transmit",
            exceed_action="transmit",
            violate_action=action,
            violate_mark_cos=3 if mark else None,
            violate_mark_dscp=10 if mark else None,
            description=f"Violate action {action}.",
        )
    cfg.push(live_aci)


def test_qos_requirements(live_aci: Niwaki) -> None:
    # QoS requirements marking DSCP and steering ingress/egress policers via the verbs.
    cfg = tenant(TN, description=TN_DESC)
    cfg.dpp_policy(
        QOS_DPP_IN,
        admin_st="enabled",
        type="1R2C",
        rate=1_000_000,
        rate_unit="kilo",
        confirm_action="transmit",
        exceed_action="drop",
        description="Ingress policer.",
    )
    cfg.dpp_policy(
        QOS_DPP_OUT,
        admin_st="enabled",
        type="2R3C",
        rate=2_000_000,
        rate_unit="kilo",
        peak_rate=4_000_000,
        peak_rate_unit="kilo",
        confirm_action="transmit",
        exceed_action="mark",
        exceed_mark_dscp=10,
        violate_action="drop",
        description="Egress policer.",
    )
    for i, name in enumerate(QOS_REQS):
        qos = cfg.qos_requirement(name, description=f"QoS requirement {name}.")
        qos.dscp_marking(mark=46 if i == 0 else 34, description="Re-mark matched endpoints.")
        qos.ingress_dpp(QOS_DPP_IN)
        qos.egress_dpp(QOS_DPP_OUT)
    cfg.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    dns = [f"uni/tn-{TN}/qosdpppol-{d}" for d in DPP_CART]
    dns += [f"uni/tn-{TN}/qosdpppol-{d}" for d in DPP_RATE_UNITS]
    dns += [f"uni/tn-{TN}/qosdpppol-{d}" for d in DPP_BURST_UNITS]
    dns += [f"uni/tn-{TN}/qosdpppol-{d}" for d in DPP_ACTIONS]
    dns += [f"uni/tn-{TN}/qosdpppol-{QOS_DPP_IN}", f"uni/tn-{TN}/qosdpppol-{QOS_DPP_OUT}"]
    dns += [f"uni/tn-{TN}/qosreq-{r}" for r in QOS_REQS]
    for dn in dns:
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
