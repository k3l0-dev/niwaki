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
    BdSubnetCursor,
    EpgCursor,
    TenantCursor,
    TenantFilterCursor,
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
        assert type(cfg.bd("web2").subnet("10.0.0.1/24")) is BdSubnetCursor

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
        """Implicit pop is statically visible: EpgCursor exposes tenant makers.

        Ancestor makers come from the inherited maker mixins (one per ancestor
        level) — the method must resolve through the MRO with its typed
        signature, wherever it is defined.
        """
        for maker in ("epg", "app", "bd", "vrf", "l3out", "filter", "contract"):
            method = getattr(EpgCursor, maker, None)
            assert callable(method), f"EpgCursor lacks typed {maker}()"
            assert "params" in method.__code__.co_varnames, f"{maker}() is not a generated maker"

    def test_mixin_mro_is_nearest_wins(self) -> None:
        """The mixin inheritance order reproduces the runtime resolution:
        a maker defined at a nearer level shadows the same label further up."""
        mro_names = [c.__name__ for c in EpgCursor.__mro__]
        app_idx = mro_names.index("_AppMakers")
        tenant_idx = mro_names.index("_TenantMakers")
        uni_idx = mro_names.index("_UniMakers")
        assert app_idx < tenant_idx < uni_idx

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
        params = inspect.signature(BdSubnetCursor.bind).parameters
        assert "vrf" in params

    def test_entry_maker_exposes_sugar_params(self) -> None:
        params = inspect.signature(TenantFilterCursor.entry).parameters
        assert "tcp" in params
        assert "udp" in params
        assert "destination_from_port" in params

    def test_epg_verbs_typed(self) -> None:
        assert "provide" in EpgCursor.__dict__
        assert "consume" in EpgCursor.__dict__

    def test_non_creatable_singleton_maker_defaults_its_name(self) -> None:
        """A maker for a non-creatable, name-only singleton defaults ``name`` to
        ``"default"`` (B1) — ``.qos_instance_policy()`` reads as the singleton it
        is — while a creatable class keeps a required name."""
        from niwaki.design._generated_cursors import InfraCursor

        singleton = inspect.signature(InfraCursor.qos_instance_policy).parameters["name"]
        assert singleton.default == "default"
        creatable = inspect.signature(InfraCursor.cdp_policy).parameters["name"]
        assert creatable.default is inspect.Parameter.empty

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
        # Fabric also has a spine_selector → node_block path, so the infra one
        # disambiguates on its profile-level ancestor.
        assert spine.__name__ == "SpineProfileSpineSelectorNodeBlockCursor"

    def test_regeneration_matches_curation(self) -> None:
        """render_all() stays parseable and emits one cursor per curated position."""
        from niwaki._codegen.generate_design import render_all

        classes: set[str] = set()
        for content in render_all().values():
            tree = ast.parse(content)
            classes |= {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
        cursors = {name for name in classes if not name.startswith("_")}
        expected = {type_.__name__ for type_ in CURSOR_FOR.values()}
        assert cursors == expected

    def test_committed_package_matches_regeneration(self) -> None:
        """A stale committed module means vocabulary.yaml changed without
        running ``uv run python -m niwaki._codegen.generate_design``.

        The committed files are ruff-formatted after generation, so the guard
        normalises whitespace-only differences by comparing the AST dumps.
        """
        from niwaki._codegen.generate_design import OUTPUT_DIR, render_all

        rendered = render_all()
        on_disk = {p.name for p in OUTPUT_DIR.glob("*.py")}
        assert on_disk == set(rendered), "module set drifted — re-run generate_design"
        for filename, content in rendered.items():
            disk = (OUTPUT_DIR / filename).read_text(encoding="utf-8")
            assert ast.dump(ast.parse(disk)) == ast.dump(ast.parse(content)), (
                f"{filename} is stale — re-run generate_design"
            )


class TestMakerDocumentation:
    """Generated makers carry Cisco-documented Args sections."""

    def test_maker_docstring_has_args_section(self) -> None:
        from niwaki.design._generated_cursors._tenant import TenantCursor

        doc = " ".join((TenantCursor.ospf_interface_policy.__doc__ or "").split())
        assert "Args:" in doc
        assert "point-to-point and broadcast" in doc  # Cisco's field comment
        assert "Values: ``bcast``, ``p2p``, ``unspecified``" in doc
        assert "Default: ``unspecified``" in doc

    def test_maker_docstring_carries_class_definition(self) -> None:
        from niwaki.design._generated_cursors._tenant import TenantCursor

        doc = " ".join((TenantCursor.bd.__doc__ or "").split())
        assert doc.startswith("Declare a ``fvBD`` child")
        assert "unique layer 2 forwarding domain" in doc

    def test_sugar_params_are_documented(self) -> None:
        doc = TenantFilterCursor.entry.__doc__ or ""
        assert "tcp: Curated shorthand" in doc
