"""Wire aliases of fields renamed by the 2.0 naming wave.

The 2.0 acceptance gate renamed 568 model fields; every rename must leave the
serialization alias — the wire name the APIC actually receives — untouched.
The exhaustive parity nets resolve names *through* the alias, so an alias a
generator bug dropped or typoed would make them skip the field rather than
fail.  This golden drives renamed kwargs end-to-end through the design
compiler and pins the literal wire keys in the emitted payload.
"""

from __future__ import annotations

from typing import Any

from niwaki.design import infra, tenant


def _find_class(node: dict[str, Any], aci_class: str) -> dict[str, Any] | None:
    """Return the attributes dict of the first ``aci_class`` node in a payload."""
    for cls, body in node.items():
        if cls == aci_class:
            return dict(body.get("attributes", {}))
        for child in body.get("children", []):
            found = _find_class(child, aci_class)
            if found is not None:
                return found
    return None


class TestRenamedFieldsKeepTheirWireAlias:
    def test_l3out_family_scopemeta_renames(self) -> None:
        # rtrId → router_id, addr → ip_address, enforceRtctrl →
        # enforce_route_control: the renames from the recovered l3ext
        # scopemeta labels.  The wire keys must be the originals.
        t = tenant("wa")
        out = t.l3out("edge", enforce_route_control="export,import")
        np = out.node_profile("np")
        np.node_attachment("topology/pod-1/node-101", router_id="1.1.1.1")
        np.interface_profile("ifp").path_attachment(
            "topology/pod-1/paths-101/pathep-[eth1/1]",
            if_inst_t="l3-port",
            ip_address="192.0.2.1/30",
        )
        payload = t.to_payload()

        l3out_attrs = _find_class(payload, "l3extOut")
        assert l3out_attrs is not None
        # Flags serialize in canonical member order — compare as a set.
        assert set(l3out_attrs["enforceRtctrl"].split(",")) == {"export", "import"}
        assert "enforce_route_control" not in l3out_attrs

        node_attrs = _find_class(payload, "l3extRsNodeL3OutAtt")
        assert node_attrs is not None
        assert node_attrs["rtrId"] == "1.1.1.1"
        assert "router_id" not in node_attrs

        path_attrs = _find_class(payload, "l3extRsPathL3OutAtt")
        assert path_attrs is not None
        assert path_attrs["addr"] == "192.0.2.1/30"
        assert "ip_address" not in path_attrs

    def test_sentence_fix_renames(self) -> None:
        # collectIntvl → collect_intvl (netflowNodePol): a sentence-label
        # cleanup rename; the wire key must be the original camelCase.
        cfg = infra()
        cfg.netflow_node_policy("nf", collect_intvl=300)
        payload = cfg.to_payload()

        nf_attrs = _find_class(payload, "netflowNodePol")
        assert nf_attrs is not None
        assert nf_attrs["collectIntvl"] == "300"
        assert "collect_intvl" not in nf_attrs
