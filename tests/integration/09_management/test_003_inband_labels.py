"""Management — in-band EPG contract/subject labels (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/09_management/test_003_inband_labels.py -m integration -s

An in-band management EPG can carry six kinds of label — provider / consumer,
provider-subject / consumer-subject, provider-contract / consumer-contract. This
file exercises **all 140 policy colours** the ``tag`` enum offers, distributed
across the six makers, and flips ``complement`` both ways on the three label
kinds that expose it. The result is one in-band EPG wearing every label colour at
least once.

Everything is **named** (``niwaki-it-*``) under the APIC-managed ``mgmt`` tenant;
the tenant and its ``mgmtp-default`` profile are only *traversed*. Values are
illustrative.

``wipe(aci)`` (operator-only) removes only the named objects this file creates.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import Cursor, tenant
from niwaki.exceptions import NotFoundError
from niwaki.models.aaa.aaaRbacAnnotation import aaaRbacAnnotation
from niwaki.models.enums.PolColor import PolColor
from niwaki.models.tag.tagAnnotation import tagAnnotation
from niwaki.models.tag.tagTag import tagTag

pytestmark = pytest.mark.integration

TN = "mgmt"
EPG = "niwaki-it-inb-labels"
ENCAP = "vlan-2951"

# Every policy colour the tag enum offers, split into six even slices — one per
# label maker — so all 140 values are exercised at least once.
COLORS = [c.value for c in PolColor]


def _common(obj: Cursor) -> None:
    """Attach the universal children present on almost every ACI class."""
    obj.mo(tagTag, key="niwaki-it", value="management")
    obj.mo(tagAnnotation, key="niwaki-it-note", value="exhaustive-provisioning")
    obj.mo(aaaRbacAnnotation, domain="all")


def test_inband_labels(live_aci: Niwaki) -> None:
    mgmt = tenant(TN)
    profile = mgmt.management_profile()
    epg = profile.in_band_epg(
        EPG, encap=ENCAP, description="In-band EPG wearing every label colour."
    )
    _common(epg)

    # provider / consumer labels (with complement flipped) — colours [0:48].
    for i, color in enumerate(COLORS[0:24]):
        epg.provider_label(
            f"niwaki-it-pl-{i:03d}",
            tag=color,
            complement=(i % 2 == 0),
            description=f"Provider {color}.",
        )
    for i, color in enumerate(COLORS[24:48]):
        epg.consumer_label(
            f"niwaki-it-cl-{i:03d}",
            tag=color,
            description=f"Consumer {color}.",
        )

    # provider / consumer subject labels (with complement flipped) — colours [48:96].
    for i, color in enumerate(COLORS[48:72]):
        epg.provider_subject_label(
            f"niwaki-it-psl-{i:03d}",
            tag=color,
            complement=(i % 2 == 0),
            description=f"Provider subject {color}.",
        )
    for i, color in enumerate(COLORS[72:96]):
        epg.consumer_subject_label(
            f"niwaki-it-csl-{i:03d}",
            tag=color,
            complement=(i % 2 == 0),
            description=f"Consumer subject {color}.",
        )

    # provider / consumer contract labels (no complement) — colours [96:140].
    for i, color in enumerate(COLORS[96:118]):
        epg.provider_contract_label(
            f"niwaki-it-pcl-{i:03d}", tag=color, description=f"Provider contract {color}."
        )
    for i, color in enumerate(COLORS[118:140]):
        epg.consumer_contract_label(
            f"niwaki-it-ccl-{i:03d}", tag=color, description=f"Consumer contract {color}."
        )

    mgmt.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (f"uni/tn-{TN}/mgmtp-default/inb-{EPG}",):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
