"""Tenant contracts — the six label kinds and contract exceptions (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/05_contracts/test_007_labels_exceptions.py -m integration -s

Labels classify who may provide/consume a contract. This file exercises all six
label classes on a VRF's ``vzAny`` collection — provider / consumer, provider- /
consumer-subject, provider- / consumer-contract — cycling through **every** policy
colour the schema offers and toggling the complement flag on the kinds that carry it.
A second pass exercises ``vzException`` across every match field (Ctx / Dn / EPg /
Tag / Tenant) at both the contract and subject levels.

Values are illustrative — this proves the SDK expresses the label/exception surface,
not a production policy. ``wipe(aci)`` (operator-only) removes what this file owns.
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

LBL_VRF = "niwaki-it-lbl-vrf"
EXC_FLT = "niwaki-it-exc-flt"
EXC_CTR = "niwaki-it-exc-ctr"

EXC_FIELDS = ("Ctx", "Dn", "EPg", "Tag", "Tenant")

# Every PolColor value (from the generated enum) — spread across the six label kinds.
COLORS = (
    "alice-blue", "antique-white", "aqua", "aquamarine", "azure", "beige", "bisque", "black",
    "blanched-almond", "blue", "blue-violet", "brown", "burlywood", "cadet-blue", "chartreuse",
    "chocolate", "coral", "cornflower-blue", "cornsilk", "crimson", "cyan", "dark-blue",
    "dark-cyan", "dark-goldenrod", "dark-gray", "dark-green", "dark-khaki", "dark-magenta",
    "dark-olive-green", "dark-orange", "dark-orchid", "dark-red", "dark-salmon", "dark-sea-green",
    "dark-slate-blue", "dark-slate-gray", "dark-turquoise", "dark-violet", "deep-pink",
    "deep-sky-blue", "dim-gray", "dodger-blue", "fire-brick", "floral-white", "forest-green",
    "fuchsia", "gainsboro", "ghost-white", "gold", "goldenrod", "gray", "green", "green-yellow",
    "honeydew", "hot-pink", "indian-red", "indigo", "ivory", "khaki", "lavender", "lavender-blush",
    "lawn-green", "lemon-chiffon", "light-blue", "light-coral", "light-cyan",
    "light-goldenrod-yellow", "light-gray", "light-green", "light-pink", "light-salmon",
    "light-sea-green", "light-sky-blue", "light-slate-gray", "light-steel-blue", "light-yellow",
    "lime", "lime-green", "linen", "magenta", "maroon", "medium-aquamarine", "medium-blue",
    "medium-orchid", "medium-purple", "medium-sea-green", "medium-slate-blue",
    "medium-spring-green", "medium-turquoise", "medium-violet-red", "midnight-blue", "mint-cream",
    "misty-rose", "moccasin", "navajo-white", "navy", "old-lace", "olive", "olive-drab", "orange",
    "orange-red", "orchid", "pale-goldenrod", "pale-green", "pale-turquoise", "pale-violet-red",
    "papaya-whip", "peachpuff", "peru", "pink", "plum", "powder-blue", "purple", "red",
    "rosy-brown", "royal-blue", "saddle-brown", "salmon", "sandy-brown", "sea-green", "seashell",
    "sienna", "silver", "sky-blue", "slate-blue", "slate-gray", "snow", "spring-green",
    "steel-blue", "tan", "teal", "thistle", "tomato", "turquoise", "violet", "wheat", "white",
    "white-smoke", "yellow", "yellow-green",
)  # fmt: skip


def test_vzany_labels(live_aci: Niwaki) -> None:
    # All six label classes on a vzAny, cycling every colour; complement toggles on
    # the provider / provider-subject / consumer-subject kinds that carry it.
    cfg = tenant(TN, description=TN_DESC)
    vzany = cfg.vrf(LBL_VRF, description="VRF hosting the label cartesian.").vzany(
        description="vzAny carrying every label kind."
    )
    for i, color in enumerate(COLORS[0::6]):
        vzany.provider_label(
            f"pl-{color}", tag=color, complement=bool(i % 2), description=f"Provider label {color}."
        )
    for color in COLORS[1::6]:
        vzany.consumer_label(f"cl-{color}", tag=color, description=f"Consumer label {color}.")
    for i, color in enumerate(COLORS[2::6]):
        vzany.provider_subject_label(
            f"psl-{color}",
            tag=color,
            complement=bool(i % 2),
            description=f"Provider subject label {color}.",
        )
    for i, color in enumerate(COLORS[3::6]):
        vzany.consumer_subject_label(
            f"csl-{color}",
            tag=color,
            complement=bool(i % 2),
            description=f"Consumer subject label {color}.",
        )
    for color in COLORS[4::6]:
        vzany.provider_contract_label(
            f"pcl-{color}", tag=color, description=f"Provider contract label {color}."
        )
    for color in COLORS[5::6]:
        vzany.consumer_contract_label(
            f"ccl-{color}", tag=color, description=f"Consumer contract label {color}."
        )
    cfg.push(live_aci)


def test_exceptions(live_aci: Niwaki) -> None:
    # vzException across every match field, at both the contract and subject levels.
    cfg = tenant(TN, description=TN_DESC)
    cfg.filter(EXC_FLT, description="Filter for the exception contract.").entry(
        "e-https", tcp=443, description="HTTPS."
    )
    ctr = cfg.contract(EXC_CTR, scope="context", description="Contract carrying exceptions.")
    subj = ctr.subject(
        "subj", reverse_filter_ports=True, description="Subject carrying exceptions."
    )
    subj.bind(filter=EXC_FLT)
    for field in EXC_FIELDS:
        ctr.exception(
            f"exc-ctr-{field.lower()}",
            field=field,
            prov_regex=f"prov-{field.lower()}-.*",
            cons_regex=f"cons-{field.lower()}-.*",
        )
        subj.exception(
            f"exc-subj-{field.lower()}",
            field=field,
            prov_regex=f"sprov-{field.lower()}-.*",
        )
    cfg.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (
        f"uni/tn-{TN}/ctx-{LBL_VRF}",  # cascades the vzAny and its labels
        f"uni/tn-{TN}/brc-{EXC_CTR}",
        f"uni/tn-{TN}/flt-{EXC_FLT}",
    ):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
