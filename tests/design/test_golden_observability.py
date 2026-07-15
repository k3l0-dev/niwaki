"""Golden wire format — SPAN, NetFlow and QoS requirements.

Offline counterpart of the observability act.  Three things are pinned here:

* the **same makers occupy three positions** — a SPAN source group hangs under
  the tenant, under infra (access SPAN) and under fabric alike;
* a SPAN source group declared in a tenant reaches a filter group declared
  under infra: one design, **two domains**, one closed-world resolution;
* ``qosRequirement`` carries **two relations to the same class** (ingress and
  egress policing).  Automatic resolution cannot choose between them, so the
  curated verbs name their Rs class — the mechanism vzAny uses.
"""

from __future__ import annotations

from niwaki.design import Cursor, design
from niwaki.design._compiler import compile_ops
from niwaki.design._resolver import resolve


def observability() -> Cursor:
    """One design spanning infra and tenant."""
    cfg = design()

    infra = cfg.infra()
    # Ports written as 0 — the APIC stores that as "unspecified", and so do we:
    # the RN, hence the DN, is built from the value the fabric will hold.
    infra.filter_group("fg").filter_entry("tcp", "10.0.0.0/24", "10.0.1.0/24", 0, 0, 0, 0)
    infra.netflow_record("rec")
    infra.netflow_exporter("exp", remote_entity_ip="10.9.9.9", remote_entity_l4_port="2055")
    infra.netflow_monitor("mon").bind(netflow_record="rec", netflow_exporter="exp")

    tenant = cfg.tenant("T")
    tenant.vrf("v")
    tenant.bd("b").bind(vrf="v")
    tenant.dpp_policy("dpp", rate=100)

    qos = tenant.qos_requirement("qr")
    qos.dscp_marking(mark="AF11")
    qos.ingress_dpp("dpp").egress_dpp("dpp")

    tenant.app("ap").epg("web").bind(bd="b", qos_requirement="qr")

    src = tenant.span_source_group("span-src", administrative_state="enabled")
    src.bind(filter_group="fg")  # declared under infra — a cross-domain bind
    src.span_label("span-dst")  # matches the destination group by name
    src.span_source("src-web", direction_ingress_egress_both="both").bind(epg="web")

    tenant.span_destination_group("span-dst").span_destination("dst").bind(epg="web")
    return cfg


def _flatten(cursor: Cursor) -> dict[str, dict[str, str]]:
    """The DN → attributes map the push engine writes, minus the ``dn`` echo."""
    root = cursor.design_node.root()
    flat = {}
    for op in compile_ops(root, resolve(root)):
        assert op.payload is not None
        ((_, body),) = op.payload.items()
        flat[op.dn] = {k: v for k, v in body["attributes"].items() if k != "dn"}
    return flat


class TestObservabilityGolden:
    def test_span_crosses_domains(self) -> None:
        """The tenant's SPAN source group points at an infra filter group."""
        flat = _flatten(observability())
        src = "uni/tn-T/srcgrp-span-src"
        assert {dn: a for dn, a in flat.items() if dn.startswith(src)} == {
            src: {"name": "span-src", "adminSt": "enabled"},
            f"{src}/rssrcGrpToFilterGrp": {"tDn": "uni/infra/filtergrp-fg"},
            f"{src}/spanlbl-span-dst": {"name": "span-dst"},
            f"{src}/src-src-web": {"name": "src-web", "dir": "both"},
            f"{src}/src-src-web/rssrcToEpg": {"tDn": "uni/tn-T/ap-ap/epg-web"},
        }
        assert flat["uni/tn-T/destgrp-span-dst/dest-dst/rsdestEpg"] == {
            "tDn": "uni/tn-T/ap-ap/epg-web"
        }

    def test_span_filter_group_lives_under_infra(self) -> None:
        """Seven naming props — and four of them are numbers the APIC renames.

        A port of 0 is stored as ``"unspecified"``, and the RN is built from the
        naming values: a model that kept the number would compute a DN the fabric
        does not have, and ``push`` would create a second entry beside the first.
        """
        flat = _flatten(observability())
        entry = (
            "uni/infra/filtergrp-fg/proto-tcp-src-[10.0.0.0/24]-dst-[10.0.1.0/24]"
            "-srcPortFrom-unspecified-srcPortTo-unspecified"
            "-dstPortFrom-unspecified-dstPortTo-unspecified"
        )
        assert flat[entry] == {
            "ipProto": "tcp",
            "srcAddr": "10.0.0.0/24",
            "dstAddr": "10.0.1.0/24",
            "srcPortFrom": "unspecified",
            "srcPortTo": "unspecified",
            "dstPortFrom": "unspecified",
            "dstPortTo": "unspecified",
        }

    def test_netflow_monitor_binds_exporter_and_record(self) -> None:
        flat = _flatten(observability())
        assert flat["uni/infra/monitorpol-mon/rsmonitorToExporter-exp"] == {
            "tnNetflowExporterPolName": "exp"
        }
        assert flat["uni/infra/monitorpol-mon/rsmonitorToRecord"] == {
            "tnNetflowRecordPolName": "rec"
        }
        assert flat["uni/infra/exporterpol-exp"] == {
            "name": "exp",
            "dstAddr": "10.9.9.9",
            "dstPort": "2055",
        }

    def test_ingress_and_egress_policing_are_told_apart(self) -> None:
        """Two relations, one target class — the verb names the Rs class."""
        flat = _flatten(observability())
        qr = "uni/tn-T/qosreq-qr"
        assert {dn: a for dn, a in flat.items() if dn.startswith(qr)} == {
            qr: {"name": "qr"},
            f"{qr}/dscp_marking": {"mark": "AF11"},
            f"{qr}/rsingressDppPol": {"tnQosDppPolName": "dpp"},
            f"{qr}/rsegressDppPol": {"tnQosDppPolName": "dpp"},
        }
        # And the EPG reaches the requirement — the bind the EPG wave left open.
        assert flat["uni/tn-T/ap-ap/epg-web/rsqosRequirement"] == {"tnQosRequirementName": "qr"}

    def test_the_same_makers_serve_three_domains(self) -> None:
        """A SPAN source group is curated under tenant, infra and fabric."""
        cfg = design()
        cfg.infra().span_source_group("access-span")
        cfg.fabric().span_source_group("fabric-span")
        cfg.tenant("T").span_source_group("tenant-span")
        flat = _flatten(cfg)
        assert {dn for dn in flat if "srcgrp-" in dn} == {
            "uni/infra/srcgrp-access-span",
            "uni/fabric/srcgrp-fabric-span",
            "uni/tn-T/srcgrp-tenant-span",
        }
