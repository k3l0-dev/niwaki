"""Fabric — SPAN destination groups (exhaustive combinations, non-prod).

Run:
    uv run pytest tests/integration/03_fabric/test_015_span_destinations.py -m integration -s

A span destination group holds a single destination, so combination coverage is
spread across many groups: local-port destinations on fabric-discovered ports,
and ERSPAN-to-EPG destinations covering both ERSPAN versions, a spread of DSCP
markings and both visibility modes. Each ERSPAN destination sets its analyser IP
and source-prefix on the destination relation (via ``ref``) and carries the
ERSPAN encapsulation summary.

Exhaustive combination coverage, illustrative values — not a real fabric config.
The APIC itself is not accepted as a SPAN destination on this fabric, so only
local-port and ERSPAN-to-EPG destinations are provisioned.

``wipe(aci)`` (operator-only) removes every span destination group.
"""

from __future__ import annotations

import contextlib

import pytest

from niwaki import Niwaki
from niwaki.design import fabric, ref
from niwaki.exceptions import NotFoundError

pytestmark = pytest.mark.integration

INB_MGMT_EPG = "uni/tn-mgmt/mgmtp-default/inb-default"

LOCAL_PREFIX = "niwaki-it-spandst-local"
ERSPAN_PREFIX = "niwaki-it-spandst-erspan"
LOCAL_PORTS = ("eth1/45", "eth1/46", "eth1/47")
# (version, mode, dscp) — one ERSPAN destination group each.
ERSPAN_SPECS = (
    ("ver1", "visible", "CS0"),
    ("ver2", "not-visible", "CS4"),
    ("ver1", "visible", "AF11"),
    ("ver2", "not-visible", "AF41"),
    ("ver1", "visible", "EF"),
    ("ver2", "not-visible", "VA"),
    ("ver1", "not-visible", "CS6"),
    ("ver2", "visible", "unspecified"),
)


def test_local_span_destinations(live_aci: Niwaki) -> None:
    leaf = _first_leaf(live_aci)
    fab = fabric()
    for idx, port in enumerate(LOCAL_PORTS):
        group = fab.span_destination_group(
            f"{LOCAL_PREFIX}-{idx}",
            description=f"Local SPAN destination on {port}.",
        )
        group.span_destination(
            "dest",
            description=f"Local SPAN to {port}.",
        ).bind_dn(path=_path_dn(leaf, port))
    fab.push(live_aci)


def test_erspan_destinations(live_aci: Niwaki) -> None:
    fab = fabric()
    for idx, (version, mode, dscp) in enumerate(ERSPAN_SPECS):
        group = fab.span_destination_group(
            f"{ERSPAN_PREFIX}-{idx}",
            description=f"ERSPAN destination {version}, DSCP {dscp}, {mode}.",
        )
        dest = group.span_destination(
            "dest",
            description=f"ERSPAN to EPG, {version}, DSCP {dscp}.",
        ).bind_dn(
            epg=ref(
                INB_MGMT_EPG,
                ip=f"192.0.2.{50 + idx}",
                src_ip_prefix="192.0.2.0/24",
                dscp=dscp,
                ver=version,
                mtu=1518,
                ttl=64,
            )
        )
        dest.vspan_epg_summary(
            description=f"ERSPAN summary, DSCP {dscp}, {mode}.",
            destination_ip=f"192.0.2.{50 + idx}",
            source_ip_of_erspan_packet="192.0.2.0/24",
            dscp=dscp,
            mode=mode,
            mtu=1518,
            time_to_live=64,
            flow_id=idx + 1,
        )
    fab.push(live_aci)


def _first_leaf(aci: Niwaki) -> str:
    """DN of the lowest-numbered leaf discovered in the fabric."""
    found: list[tuple[int, str]] = []
    for node in aci.query("fabricNode").fetch():
        data = node.model_dump(by_alias=True)
        if data.get("role") == "leaf" and data.get("id") and data.get("dn"):
            found.append((int(data["id"]), str(data["dn"])))
    return sorted(found)[0][1]


def _path_dn(node_dn: str, interface: str) -> str:
    """Build a ``fabricPathEp`` DN for ``interface`` on the switch at ``node_dn``."""
    node_id = node_dn.rsplit("/node-", 1)[1]
    prefix = node_dn.rsplit("/node-", 1)[0]
    return f"{prefix}/paths-{node_id}/pathep-[{interface}]"


def wipe(aci: Niwaki) -> None:
    """MANUAL teardown — run by the operator only; the suite never calls it."""
    dns = [f"uni/fabric/destgrp-{LOCAL_PREFIX}-{i}" for i in range(len(LOCAL_PORTS))]
    dns += [f"uni/fabric/destgrp-{ERSPAN_PREFIX}-{i}" for i in range(len(ERSPAN_SPECS))]
    for dn in dns:
        with contextlib.suppress(NotFoundError):
            aci.node(dn).delete()
