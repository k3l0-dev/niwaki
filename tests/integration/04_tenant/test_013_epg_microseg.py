"""Tenant — EPG micro-segmentation, combination-exhaustive (non-prod).

Run:
    uv run pytest tests/integration/04_tenant/test_013_epg_microseg.py -m integration -s

Attribute-based (micro-segmented) EPGs and their match criteria. One criterion
carries the **full cartesian of every VM attribute type x every operator**;
another carries IP (explicit and use-subnet), MAC and DNS attributes with a
nested sub-criterion; a third exercises the uSeg-BD association. Criteria cover
both matching-rule types (any / all).

Values are illustrative. The identity-group attribute (fvIdGroupAttr) needs a
cloud/NDO endpoint-group DN and is not exercised on the on-prem simulator
(platform limitation, not a coverage gap). Match precedence is set only on
criteria without IP/MAC attributes; uSeg criteria are BD-scoped (VRF scope is
unsupported). This file owns tenant ``niwaki-it-useg``; ``wipe`` (operator-only)
deletes it.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import tenant
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

TN = "niwaki-it-useg"
VRF = "niwaki-it-useg-vrf"
BD = "niwaki-it-useg-bd"
APP = "niwaki-it-useg-app"

VM_TYPES = [
    "custom-label",
    "domain",
    "guest-os",
    "hv",
    "rootContName",
    "tag",
    "vm",
    "vm-folder",
    "vm-name",
    "vmfolder-path",
    "vnic",
]
OPERATORS = ["contains", "endsWith", "equals", "notEquals", "startsWith"]


def _foundation(tn):  # type: ignore[no-untyped-def]
    tn.vrf(VRF, description="VRF backing the micro-segmented EPGs.")
    tn.bd(BD, description="BD backing the micro-segmented EPGs.", unicast_routing=True).bind(
        vrf=VRF
    )


def test_microseg_vm_full(live_aci: Niwaki) -> None:
    """A uSeg EPG whose criterion carries every VM attribute type x operator."""
    tn = tenant(
        TN, description="Micro-segmentation criteria - full VM attribute type and operator matrix"
    )
    _foundation(tn)
    crtrn = (
        tn.app(APP, description="Application profile for micro-segmentation.")
        .epg(
            "niwaki-it-epg-useg-vm",
            description="uSeg EPG matched by the full VM-attribute matrix.",
            attribute_based_epg=True,
        )
        .bind(bd=BD)
        .criterion(
            description="BD-scoped criterion, match all, with precedence.",
            matching_rule_type="all",
            criterion_scope="scope-bd",
            precedence=10,
        )
    )
    index = 0
    for attr_type in VM_TYPES:
        for operator in OPERATORS:
            crtrn.vm_attribute(
                f"niwaki-it-vm-{index:02d}",
                description=f"VM attribute: {attr_type} {operator}.",
                attribute_type=attr_type,
                operator=operator,
                value=f"val-{index:02d}",
                tag_category="environment" if attr_type == "tag" else None,
                custom_attribute_name="owner" if attr_type == "custom-label" else None,
            )
            index += 1

    tn.push(live_aci)


def test_microseg_ip_mac_dns(live_aci: Niwaki) -> None:
    """A uSeg EPG with IP / MAC / DNS attributes and a nested sub-criterion."""
    tn = tenant(
        TN, description="Micro-segmentation criteria - full VM attribute type and operator matrix"
    )
    _foundation(tn)
    crtrn = (
        tn.app(APP)
        .epg(
            "niwaki-it-epg-useg-ipmac",
            description="uSeg EPG matched by IP / MAC / DNS.",
            attribute_based_epg=True,
        )
        .bind(bd=BD)
        .criterion(
            description="BD-scoped criterion matching any attribute.",
            matching_rule_type="any",
            criterion_scope="scope-bd",
        )
    )
    crtrn.ip_attribute(
        "niwaki-it-ip-explicit",
        description="IP attribute, explicit address.",
        ip_address="10.32.1.5",
    )
    crtrn.ip_attribute(
        "niwaki-it-ip-subnet",
        description="IP attribute, use subnet address.",
        use_fvsubnet_address=True,
    )
    crtrn.mac_attribute(
        "niwaki-it-mac", description="MAC attribute selector.", macaddress="00:32:CC:00:00:01"
    )
    crtrn.dns_attribute(
        "niwaki-it-dns", description="DNS attribute selector.", domain_name_filter="*.corp.local"
    )
    crtrn.sub_criterion(
        "niwaki-it-sub",
        description="Nested sub-criterion matching all of its attributes.",
        matching_rule_type="all",
    ).vm_attribute(
        "niwaki-it-sub-vm",
        description="Sub-criterion VM attribute.",
        attribute_type="vm-name",
        operator="equals",
        value="db-01",
    )

    tn.push(live_aci)


def test_microseg_useg_bd(live_aci: Niwaki) -> None:
    """The uSeg-BD association, isolated from the main criteria."""
    tn = tenant(
        TN, description="Micro-segmentation criteria - full VM attribute type and operator matrix"
    )
    _foundation(tn)
    crtrn = (
        tn.app(APP)
        .epg(
            "niwaki-it-epg-useg-bd",
            description="uSeg EPG exercising the uSeg-BD association.",
            attribute_based_epg=True,
        )
        .bind(bd=BD)
        .criterion(matching_rule_type="any")
    )
    crtrn.ip_attribute("niwaki-it-usegbd-ip", ip_address="10.32.4.5")
    crtrn.useg_bd().associated_bd(BD, description="Bridge domain associated with the criterion.")

    tn.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    for dn in (f"uni/tn-{TN}",):
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
