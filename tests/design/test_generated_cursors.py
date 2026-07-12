"""Generated typed cursors — runtime dispatch, signatures, consistency.

The generated classes must be what the runtime actually returns, their
signatures must expose the curated vocabulary (that is the IDE-autocompletion
contract), and regeneration must stay consistent with ``vocabulary.yaml``.
"""

from __future__ import annotations

import ast
import inspect

from niwaki.design import tenant
from niwaki.design._cursor import _tables
from niwaki.design._generated_cursors import (
    CURSOR_FOR,
    BdCursor,
    EpgCursor,
    FilterCursor,
    SubnetCursor,
    TenantCursor,
    UniCursor,
    VrfCursor,
)


class TestRuntimeDispatch:
    def test_tenant_returns_typed_root(self) -> None:
        assert type(tenant("prod")) is TenantCursor

    def test_makers_return_typed_cursors(self) -> None:
        cfg = tenant("prod")
        assert type(cfg.bd("web")) is BdCursor
        assert type(cfg.vrf("v")) is VrfCursor
        assert type(cfg.app("a").epg("e")) is EpgCursor
        assert type(cfg.bd("web2").subnet("10.0.0.1/24")) is SubnetCursor

    def test_typed_maker_prunes_none_params(self) -> None:
        bd = tenant("prod").bd("web")
        assert bd.design_node.attrs == {}

    def test_typed_maker_forwards_provided_params(self) -> None:
        bd = tenant("prod").bd("web", unicast_routing=True)
        assert bd.design_node.attrs == {"unicast_routing": True}

    def test_typed_set_merges(self) -> None:
        bd = tenant("prod").bd("web").set(arp_flooding=True).set(unicast_routing=False)
        assert bd.design_node.attrs == {"arp_flooding": True, "unicast_routing": False}

    def test_typed_bind_returns_same_type(self) -> None:
        bd = tenant("prod").bd("web")
        assert bd.bind(vrf="prod") is bd


class TestSignatures:
    """The typed surface is the autocompletion contract — pin it."""

    def test_ancestor_makers_are_real_methods(self) -> None:
        """Implicit pop is statically visible: EpgCursor exposes tenant makers."""
        for maker in ("epg", "app", "bd", "vrf", "l3out", "filter", "contract"):
            assert maker in EpgCursor.__dict__, f"EpgCursor lacks typed {maker}()"

    def test_set_exposes_model_fields(self) -> None:
        params = inspect.signature(BdCursor.set).parameters
        assert "unicast_routing" in params
        assert "arp_flooding" in params
        assert "multicast_allow" in params
        # Naming props are fixed at creation — never settable.
        assert "name" not in params

    def test_bind_exposes_curated_aliases(self) -> None:
        params = inspect.signature(BdCursor.bind).parameters
        assert set(params) == {"self", "vrf", "l3out"}

    def test_subnet_bind_inherits_bd_aliases(self) -> None:
        params = inspect.signature(SubnetCursor.bind).parameters
        assert "vrf" in params

    def test_entry_maker_exposes_sugar_params(self) -> None:
        params = inspect.signature(FilterCursor.entry).parameters
        assert "tcp" in params
        assert "udp" in params
        assert "destination_from_port" in params

    def test_epg_verbs_typed(self) -> None:
        assert "provide" in EpgCursor.__dict__
        assert "consume" in EpgCursor.__dict__

    def test_tenant_factory_is_typed(self) -> None:
        params = inspect.signature(tenant).parameters
        assert "description" in params


class TestConsistency:
    def test_registry_covers_every_curated_position(self) -> None:
        """CURSOR_FOR is keyed by position — the maker paths from polUni."""
        makers = _tables().makers
        expected: set[str] = {""}

        def _walk(parent_key: str, parent_class: str) -> None:
            for label, child in makers.get(parent_class, {}).items():
                key = f"{parent_key}.{label}" if parent_key else label
                expected.add(key)
                _walk(key, child)

        _walk("", "polUni")
        assert set(CURSOR_FOR) == expected

    def test_registry_root_is_unicursor(self) -> None:
        assert CURSOR_FOR[""] is UniCursor

    def test_same_class_two_positions_two_cursors(self) -> None:
        """infraNodeBlk under leaf vs spine selectors gets distinct cursors."""
        leaf = CURSOR_FOR["infra.leaf_profile.leaf_selector.node_block"]
        spine = CURSOR_FOR["infra.spine_profile.spine_selector.node_block"]
        assert leaf is not spine
        assert leaf.__name__ == "LeafSelectorNodeBlockCursor"
        assert spine.__name__ == "SpineSelectorNodeBlockCursor"

    def test_regeneration_matches_curation(self) -> None:
        """render() stays parseable and emits one class per curated position."""
        from niwaki._codegen.generate_design import render

        tree = ast.parse(render())
        classes = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
        expected = {type_.__name__ for type_ in CURSOR_FOR.values()}
        assert classes == expected
