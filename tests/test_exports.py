"""Top-level package surface — lazy design export and error paths."""

from __future__ import annotations

import pytest


class TestLazyTenantExport:
    def test_from_niwaki_import_tenant_works(self) -> None:
        from niwaki import tenant

        config = tenant("prod")
        assert config.design_node.aci_class == "fvTenant"

    def test_unknown_attribute_raises(self) -> None:
        import niwaki

        with pytest.raises(AttributeError, match="no attribute 'nonexistent'"):
            _ = niwaki.nonexistent

    def test_all_names_resolve(self) -> None:
        import niwaki

        for name in niwaki.__all__:
            assert getattr(niwaki, name) is not None

    def test_every_design_root_is_importable_from_niwaki(self) -> None:
        """Every root factory niwaki.design exports resolves from niwaki too.

        Regression: ``aaa`` was exported by ``niwaki.design`` but missing from
        the top-level lazy roots — ``from niwaki import aaa`` raised
        ImportError while every sibling root worked.
        """
        import niwaki
        import niwaki.design as design_pkg

        roots = {n for n in design_pkg.__all__ if n.islower() and callable(getattr(design_pkg, n))}
        assert {"aaa", "controller", "design", "fabric", "infra", "tenant"} <= roots
        # ``design`` is excluded from the identity check: the name is both the
        # sub-module and the factory, and once ``niwaki.design`` is imported the
        # import system binds the MODULE as the package attribute, shadowing the
        # lazy factory lookup. The factory stays reachable as
        # ``niwaki.design.design``.
        for name in ("aaa", "controller", "fabric", "infra", "tenant"):
            assert getattr(niwaki, name) is getattr(design_pkg, name)
