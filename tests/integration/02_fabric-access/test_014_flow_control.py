"""Fabric access — QoS flow-control interface policies (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/02_fabric-access/test_014_flow_control.py -m integration -s

The QoS flow-control shelf: link-level flow control across the receive x transmit
cartesian, priority flow control across every mode, and the slow-drain policy
across the congestion-clear-action x flush-admin cartesian (with timer variation).
Values are illustrative and cover the SDK surface, not a real QoS plan.

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

LLFC_STATES = ("off", "on")
PFC_MODES = ("auto", "off", "on")
CONG_ACTIONS = ("err-disable", "log", "off")
FLUSH_STATES = ("disabled", "enabled")


def _common(obj: Cursor) -> None:
    """Attach the universal children (annotation, tag, RBAC domain marker)."""
    obj.mo(tagAnnotation, key="orchestrator", value="niwaki-it")
    obj.mo(tagTag, key="lifecycle", value="integration")
    obj.mo(aaaRbacAnnotation, domain="all")


def _llfc_name(rcv: str, send: str) -> str:
    return f"niwaki-it-llfc-{rcv}-{send}"


def _pfc_name(mode: str) -> str:
    return f"niwaki-it-pfc-{mode}"


def _cong_slug(action: str) -> str:
    return {"err-disable": "errdis", "log": "log", "off": "off"}[action]


def _flush_slug(flush: str) -> str:
    return "flushon" if flush == "enabled" else "flushoff"


def _sd_name(action: str, flush: str) -> str:
    return f"niwaki-it-slowdrain-{_cong_slug(action)}-{_flush_slug(flush)}"


def test_llfc(live_aci: Niwaki) -> None:
    """Link-level flow control: receive x transmit cartesian."""
    fab = infra()
    for rcv in LLFC_STATES:
        for send in LLFC_STATES:
            pol = fab.llfc_interface_policy(
                _llfc_name(rcv, send),
                llfc_rcv_admin_st=rcv,
                llfc_send_admin_st=send,
                description=f"LLFC receive/send matrix - rx {rcv}, tx {send}.",
            )
            _common(pol)
    fab.push(live_aci)


def test_pfc(live_aci: Niwaki) -> None:
    """Priority flow control across every mode."""
    fab = infra()
    for mode in PFC_MODES:
        pol = fab.pfc_interface_policy(
            _pfc_name(mode),
            priority_flow_control_mode=mode,
            description=f"PFC mode sweep - {mode}.",
        )
        _common(pol)
    fab.push(live_aci)


def test_slow_drain(live_aci: Niwaki) -> None:
    """Slow-drain policy: congestion-clear-action x flush-admin cartesian."""
    fab = infra()
    idx = 0
    for action in CONG_ACTIONS:
        for flush in FLUSH_STATES:
            pol = fab.slow_drain_policy(
                _sd_name(action, flush),
                congestion_clear_action=action,
                slowdrain_flush_mode_admin_state=flush,
                congestion_detect_multiplier=(10, 5, 100)[idx % 3],
                flush_timeout_in_milliseconds=(500, 1000, 100)[idx % 3],
                description=f"Slow-drain action/flush matrix - clear {action}, flush {flush}.",
            )
            _common(pol)
            idx += 1
    fab.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    llfc_dns = [f"uni/infra/llfc-{_llfc_name(r, s)}" for r in LLFC_STATES for s in LLFC_STATES]
    pfc_dns = [f"uni/infra/pfc-{_pfc_name(m)}" for m in PFC_MODES]
    sd_dns = [f"uni/infra/qossdpol-{_sd_name(a, f)}" for a in CONG_ACTIONS for f in FLUSH_STATES]
    for dn in (*llfc_dns, *pfc_dns, *sd_dns):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
