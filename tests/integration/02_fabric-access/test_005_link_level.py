"""Fabric access — link-level (physical) interface policies (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/02_fabric-access/test_005_link_level.py -m integration -s

The physical-layer knob shelf a leaf/spine access policy group binds. This file
sweeps every enum value of the link-level policy independently: one policy per
port speed, one per FEC mode, one per auto-negotiation mode, one per physical
media type, one per EMI-retrain setting, plus debounce/DFE-delay number variation.
Values are illustrative and cover the SDK surface, not a real cabling plan.

# COVERAGE GAPS (curated child in CHILD_MAP but reachable only via .mo(), and it
# marks the parent extMngdBy=msc — deliberately not configured):
#   - external_tag_instance (tagExtMngdInst) on fabricHIfPol
#   - tag_instance (tagInst) on fabricHIfPol

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

# Every wire value of each enum on fabricHIfPol.
SPEEDS = ("100M", "1G", "10G", "25G", "40G", "50G", "100G", "200G", "400G", "unknown")
SPEED_META = ("auto", "inherit")
FEC_MODES = (
    "auto-fec",
    "cl74-fc-fec",
    "cl91-rs-fec",
    "cons16-rs-fec",
    "disable-fec",
    "ieee-rs-fec",
    "inherit",
    "kp-fec",
)
AUTONEG = ("off", "on", "on-enforce")
MEDIA_TYPES = ("auto", "sfp-10g-tx")
EMI_RETRAIN = ("disable", "enable")


def _common(obj: Cursor) -> None:
    """Attach the universal children (annotation, tag, RBAC domain marker)."""
    obj.mo(tagAnnotation, key="orchestrator", value="niwaki-it")
    obj.mo(tagTag, key="lifecycle", value="integration")
    obj.mo(aaaRbacAnnotation, domain="all")


def _slug(value: str) -> str:
    return value.lower().replace(".", "-")


def _speed_name(speed: str) -> str:
    return f"niwaki-it-ll-speed-{_slug(speed)}"


def _fec_name(fec: str) -> str:
    return f"niwaki-it-ll-fec-{fec}"


def _autoneg_name(an: str) -> str:
    return f"niwaki-it-ll-autoneg-{an}"


def _media_name(media: str) -> str:
    return f"niwaki-it-ll-media-{media}"


def _emi_name(emi: str) -> str:
    return f"niwaki-it-ll-emi-{emi}"


def test_speeds(live_aci: Niwaki) -> None:
    """One link-level policy per port speed (concrete forced + meta values)."""
    fab = infra()
    for speed in SPEEDS:
        pol = fab.link_level_policy(
            _speed_name(speed),
            auto_negotiation_on_off="off",
            speed=speed,
            fec_mode="inherit",
            description=f"Link-level speed sweep - forced {speed}.",
        )
        _common(pol)
    for speed in SPEED_META:
        pol = fab.link_level_policy(
            _speed_name(speed),
            auto_negotiation_on_off="on",
            speed=speed,
            fec_mode="inherit",
            description=f"Link-level speed sweep - {speed}, negotiation on.",
        )
        _common(pol)
    fab.push(live_aci)


def test_fec_modes(live_aci: Niwaki) -> None:
    """One link-level policy per FEC mode (at a fixed high speed)."""
    fab = infra()
    for fec in FEC_MODES:
        pol = fab.link_level_policy(
            _fec_name(fec),
            auto_negotiation_on_off="off",
            speed="100G",
            fec_mode=fec,
            description=f"Link-level FEC-mode sweep - {fec} at 100G.",
        )
        _common(pol)
    fab.push(live_aci)


def test_negotiation_media_emi(live_aci: Niwaki) -> None:
    """One policy per auto-negotiation mode, media type and EMI-retrain value."""
    fab = infra()
    for an in AUTONEG:
        pol = fab.link_level_policy(
            _autoneg_name(an),
            auto_negotiation_on_off=an,
            speed="inherit",
            description=f"Link-level auto-negotiation sweep - {an}.",
        )
        _common(pol)
    for media in MEDIA_TYPES:
        pol = fab.link_level_policy(
            _media_name(media),
            physical_media_type=media,
            speed="10G",
            auto_negotiation_on_off="off",
            description=f"Link-level media-type sweep - {media}.",
        )
        _common(pol)
    for emi in EMI_RETRAIN:
        pol = fab.link_level_policy(
            _emi_name(emi),
            enable_disable_emi_retrain=emi,
            speed="100G",
            auto_negotiation_on_off="off",
            link_debounce_interval_msec=200 if emi == "enable" else 100,
            dfe_delay_ms=1 if emi == "enable" else 0,
            description=f"Link-level EMI-retrain sweep - {emi}.",
        )
        _common(pol)
    fab.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    names: list[str] = []
    names += [_speed_name(s) for s in (*SPEEDS, *SPEED_META)]
    names += [_fec_name(f) for f in FEC_MODES]
    names += [_autoneg_name(a) for a in AUTONEG]
    names += [_media_name(m) for m in MEDIA_TYPES]
    names += [_emi_name(e) for e in EMI_RETRAIN]
    for name in names:
        with contextlib.suppress(NotFoundError):
            aci.node(f"uni/infra/hintfpol-{name}").delete()
