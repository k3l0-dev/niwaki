"""Fabric — coherent optics policies ZR-S / ZRP-S / DWDM (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/03_fabric/test_004_optics.py -m integration -s

The transceiver optics profiles applied at the fabric interface level. ZR-S is
provisioned across ``(admin-state, DWDM-carrier-grid)``; ZRP-S adds the FEC mode
and DAC rate dimensions; and DWDM is provisioned across a representative spread
of its channel numbers (the field admits 96 channels — a sample is exercised to
keep the object count readable).

Exhaustive combination coverage, illustrative values — not a real fabric config.

``wipe(aci)`` (operator-only) removes every ``niwaki-it-*`` optics policy.
"""

from __future__ import annotations

import contextlib
import itertools

import pytest

from niwaki import Niwaki
from niwaki.design import fabric
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

ZR = "niwaki-it-zr"
ZRP = "niwaki-it-zrp"
DWDM = "niwaki-it-dwdm"

ADMIN = ("enabled", "disabled")
CARRIERS = ("100MHzFrequency", "50GHzFrequency", "50GHzITUchannel", "50GHzWavelength")
# ZRP-S: only oFEC is exercised — cFEC narrows the chromatic-dispersion range to
# -2400..+2400, which the generated model's dispersion bound cannot express
# (cFEC itself is covered on the ZR-S policies below).
ZRP_FEC = ("oFEC",)
# The APIC pins the FEC/DAC pairing: oFEC must use DAC rate 1x1.25.
ZRP_DAC = ("1x1.25",)
# A representative spread across the 96 DWDM channels.
DWDM_CHANNELS = (
    "Channel1",
    "Channel10",
    "Channel20",
    "Channel30",
    "Channel40",
    "Channel50",
    "Channel60",
    "Channel70",
    "Channel80",
    "Channel90",
    "Channel96",
)


def _carrier_slug(carrier: str) -> str:
    """A name-safe slug for a DWDM carrier-grid selector value."""
    return carrier.replace("MHz", "mhz").replace("GHz", "ghz").lower()


def test_zr_policies(live_aci: Niwaki) -> None:
    fab = fabric()
    for admin, carrier in itertools.product(ADMIN, CARRIERS):
        fab.zr_policy(
            f"{ZR}-{admin}-{_carrier_slug(carrier)}",
            description=f"ZR-S optics: admin {admin}, {carrier} grid, cFEC, 16-QAM.",
            admin_st=admin,
            dwdm_carrier_grid_selector=carrier,
            fec_mode="cFEC",
            modulation="16QAM",
            dac_rate="1x1",
            muxponder_mode="1x400",
        )
    fab.push(live_aci)


def test_zrp_policies(live_aci: Niwaki) -> None:
    fab = fabric()
    for admin, fec, dac in itertools.product(ADMIN, ZRP_FEC, ZRP_DAC):
        fab.zrp_policy(
            f"{ZRP}-{admin}-{fec.lower()}-{dac.replace('.', '')}",
            description=f"ZRP-S optics: admin {admin}, {fec}, DAC {dac}.",
            admin_st=admin,
            fec_mode=fec,
            dac_rate=dac,
            dwdm_carrier_grid_selector="50GHzITUchannel",
            modulation="16QAM",
            muxponder_mode="1x400",
        )
    fab.push(live_aci)


def test_dwdm_policies(live_aci: Niwaki) -> None:
    fab = fabric()
    for channel in DWDM_CHANNELS:
        fab.dwdm_policy(
            f"{DWDM}-{channel.lower()}",
            description=f"DWDM interface profile on {channel}.",
            fcot_channel_number=channel,
        )
    fab.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    dns: list[str] = []
    for admin, carrier in itertools.product(ADMIN, CARRIERS):
        dns.append(f"uni/fabric/zrfab-{ZR}-{admin}-{_carrier_slug(carrier)}")
    for admin, fec, dac in itertools.product(ADMIN, ZRP_FEC, ZRP_DAC):
        dns.append(f"uni/fabric/zrpfab-{ZRP}-{admin}-{fec.lower()}-{dac.replace('.', '')}")
    dns += [f"uni/fabric/dwdmfabifpol-{DWDM}-{c.lower()}" for c in DWDM_CHANNELS]
    for dn in dns:
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
