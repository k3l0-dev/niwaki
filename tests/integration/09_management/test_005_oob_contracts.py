"""Management — out-of-band contract matrix (exhaustive, non-prod).

Run:
    uv run pytest tests/integration/09_management/test_005_oob_contracts.py -m integration -s

The out-of-band contract (``vzOOBBrCP``) is a management-tenant construct. This
file lays down a matrix of them so every attribute value is exercised: one
contract per target DSCP (all 23), with ``intent`` (3), ``scope`` (4) and QoS
class rotated across the set. Each carries a subject whose consumer/provider
match types, ``reverse_filter_ports``, QoS class and subject DSCP vary, bound to
one of several filters — and, on a spread of subjects, ingress/egress terminals,
subject labels and exceptions, plus a contract-level exception.

Everything is **named** (``niwaki-it-*``) under the APIC-managed ``mgmt`` tenant,
which is only *traversed*. Values are illustrative.

``wipe(aci)`` (operator-only) removes only the named objects this file creates.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import Cursor, ref, tenant
from niwaki.exceptions import NotFoundError
from niwaki.models.aaa.aaaRbacAnnotation import aaaRbacAnnotation
from niwaki.models.tag.tagAnnotation import tagAnnotation
from niwaki.models.tag.tagTag import tagTag

pytestmark = pytest.mark.integration

# COVERAGE GAPS / deliberate scoping:
#   - subject service_graph bind (vzRsSubjGraphAtt -> vnsAbsGraph): needs a
#     service graph, exercised under 07_services.

TN = "mgmt"

DSCP = (
    "AF11", "AF12", "AF13", "AF21", "AF22", "AF23", "AF31", "AF32", "AF33",
    "AF41", "AF42", "AF43", "CS0", "CS1", "CS2", "CS3", "CS4", "CS5", "CS6",
    "CS7", "EF", "VA", "unspecified",
)  # fmt: skip
INTENTS = ("install", "estimate_add", "estimate_delete")
SCOPES = ("context", "global", "tenant", "application-profile")
MATCH = ("All", "AtleastOne", "AtmostOne", "None")
PRIOS = ("level1", "level2", "level3", "level4", "level5", "level6", "unspecified")
FIELDS = ("Ctx", "Dn", "EPg", "Tag", "Tenant")

FILTERS = ("niwaki-it-oob-flt-0", "niwaki-it-oob-flt-1", "niwaki-it-oob-flt-2")
# One OOB contract per target DSCP value.
CTR_NAMES = [f"niwaki-it-oobctr-{i:02d}" for i in range(len(DSCP))]


def _common(obj: Cursor) -> None:
    """Attach the universal children present on almost every ACI class."""
    obj.mo(tagTag, key="niwaki-it", value="management")
    obj.mo(tagAnnotation, key="niwaki-it-note", value="exhaustive-provisioning")
    obj.mo(aaaRbacAnnotation, domain="all")


def test_oob_contracts(live_aci: Niwaki) -> None:
    mgmt = tenant(TN)

    # Filters the OOB subjects bind (distinct names — owned by this file only).
    ssh = mgmt.filter(FILTERS[0], description="OOB SSH filter.")
    ssh.entry(
        "ssh", tcp=22, ethernet_type="ipv4", protocol="tcp", stateful=True, description="SSH."
    )
    _common(ssh)
    snmp = mgmt.filter(FILTERS[1], description="OOB SNMP filter.")
    snmp.entry("snmp", udp=161, ethernet_type="ipv4", protocol="udp", description="SNMP.")
    _common(snmp)
    https = mgmt.filter(FILTERS[2], description="OOB HTTPS filter.")
    https.entry("https", tcp=443, ethernet_type="ip", protocol="tcp", description="HTTPS.")
    _common(https)

    for idx, dscp in enumerate(DSCP):
        name = CTR_NAMES[idx]
        ctr = mgmt.oob_contract(
            name,
            intent=INTENTS[idx % len(INTENTS)],
            scope=SCOPES[idx % len(SCOPES)],
            qos_class_id=PRIOS[idx % len(PRIOS)],
            contract_level_dscp=dscp,
            description=f"OOB contract per target-DSCP, {dscp}.",
        )
        subj = ctr.subject(
            "subj",
            consumer_label_match_type=MATCH[idx % len(MATCH)],
            provider_label_match_type=MATCH[(idx + 1) % len(MATCH)],
            reverse_filter_ports=(idx % 2 == 0),
            qos_class_id=PRIOS[(idx + 1) % len(PRIOS)],
            subject_level_dscp=DSCP[(idx + 1) % len(DSCP)],
            description=f"Subject for {name}.",
        )
        subj.bind(filter=ref(FILTERS[idx % len(FILTERS)], directives="log"))
        # Directional terminals on every third subject.
        if idx % 3 == 0:
            subj.in_term(
                description="Ingress terminal.",
                qos_class_id=PRIOS[idx % len(PRIOS)],
                terminal_level_dscp=DSCP[idx % len(DSCP)],
            )
            subj.out_term(
                description="Egress terminal.",
                qos_class_id=PRIOS[(idx + 2) % len(PRIOS)],
                terminal_level_dscp=DSCP[(idx + 2) % len(DSCP)],
            )
        # Subject labels on every other subject, complement flipped.
        if idx % 2 == 0:
            subj.provider_subject_label(
                "psl", tag="aqua", complement=(idx % 4 == 0), description="Provider subject label."
            )
            subj.consumer_subject_label(
                "csl", tag="azure", complement=(idx % 4 != 0), description="Consumer subject label."
            )
        # A subject exception and a contract exception on every fifth.
        if idx % 5 == 0:
            subj.exception("subj-exc", field=FIELDS[idx % len(FIELDS)], prov_regex="niwaki-it-.*")
            ctr.exception(
                "ctr-exc", field=FIELDS[(idx + 1) % len(FIELDS)], cons_regex="uni/tn-mgmt/.*"
            )
        _common(ctr)

    mgmt.push(live_aci)


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    dns = [f"uni/tn-{TN}/oobbrc-{name}" for name in CTR_NAMES]
    dns += [f"uni/tn-{TN}/flt-{name}" for name in FILTERS]
    for dn in dns:
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
