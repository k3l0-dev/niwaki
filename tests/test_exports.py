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
