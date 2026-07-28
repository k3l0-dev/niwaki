"""Facade resolution of deprecated navigation names (the 1.5.0 shim).

Every pre-1.5.0 navigation name in NAV_DEPRECATED must keep resolving to the
same child class as its curated replacement — with a DeprecationWarning that
names the replacement — while unknown names and write verbs keep their exact
pre-shim AttributeError behavior.
"""

from __future__ import annotations

import warnings
from importlib.util import find_spec

import pytest

from niwaki.design._cursor import _load_class
from niwaki.domain._child_map import CHILD_MAP, CLASS_PKG, NAV_DEPRECATED
from niwaki.facade import _navigate_jargon
from niwaki.models.base import ManagedObject


def _has_model(cls: str) -> bool:
    pkg = CLASS_PKG.get(cls)
    return pkg is not None and find_spec(f"niwaki.models._generated.{pkg}.{cls}") is not None


def _deprecated_positions() -> list[tuple[str, str, str]]:
    """Aliases reachable through typed facade navigation.

    Parents without a generated model can never be a typed navigation node
    (and children without one cannot be imported by ``_navigate_jargon``), so
    those aliases are exercised at the map level only
    (``test_generate_domain.test_every_alias_targets_a_live_edge_under_a_new_name``).
    """
    return [
        (parent, old_name, cls)
        for parent, aliases in NAV_DEPRECATED.items()
        if parent == "_root" or _has_model(parent)
        for old_name, cls in aliases.items()
        if _has_model(cls)
    ]


def _parent_cls(parent: str) -> type[ManagedObject]:
    return ManagedObject if parent == "_root" else _load_class(parent)


@pytest.mark.parametrize(("parent", "old_name", "cls"), _deprecated_positions())
def test_deprecated_name_resolves_identically_with_warning(
    parent: str, old_name: str, cls: str
) -> None:
    """Old name → same class as the new name, plus a DeprecationWarning."""
    parent_cls = _parent_cls(parent)
    with pytest.warns(DeprecationWarning, match="deprecated"):
        target = _navigate_jargon(parent_cls, old_name)
    assert target.child_cls.__name__ == cls

    new_name = next(n for n, c in CHILD_MAP[parent].items() if c == cls)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        fresh = _navigate_jargon(parent_cls, new_name)
    assert fresh.child_cls is target.child_cls


class TestShimBoundaries:
    @pytest.fixture(autouse=True)
    def _synthetic_alias(self, monkeypatch: pytest.MonkeyPatch):
        # The 1.5.0 aliases were retired in 1.7.0 — the shim MECHANISM stays
        # for future renames and is exercised with a synthetic entry.
        import niwaki.domain._child_map as cm

        monkeypatch.setitem(cm.NAV_DEPRECATED, "fvCtx", {"pim_ctx": "pimCtxP"})
        monkeypatch.setitem(cm.NAV_DEPRECATED, "_root", {"provider_profile": "vmmProvP"})

    def test_warning_names_the_replacement(self) -> None:
        with pytest.warns(DeprecationWarning, match="renamed 'pim'"):
            _navigate_jargon(_load_class("fvCtx"), "pim_ctx")

    def test_unknown_name_still_raises_attribute_error(self) -> None:
        with pytest.raises(AttributeError, match="no child accessor"):
            _navigate_jargon(_load_class("fvCtx"), "definitely_not_a_thing")

    def test_write_verb_still_steers_to_the_design_dsl(self) -> None:
        with pytest.raises(AttributeError, match="read-only"):
            _navigate_jargon(_load_class("fvTenant"), "create")

    def test_current_names_never_warn(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            _navigate_jargon(_load_class("fvTenant"), "bd")

    def test_warning_attributed_to_caller_on_both_entry_paths(self) -> None:
        """The warning lands on the user's line, node path and root proxy alike.

        The two paths reach _navigate_jargon through different frame depths;
        skip_file_prefixes pins attribution outside niwaki either way (a
        DeprecationWarning attributed to a library file is hidden by
        CPython's default filters — the bug this test pins).
        """
        from unittest.mock import MagicMock

        from niwaki import Niwaki

        aci = Niwaki(MagicMock())
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _ = aci.tenant("t").vrf("v").pim_ctx  # node path
            _ = aci.provider_profile  # root-proxy path (Niwaki.__getattr__)
        assert len(caught) == 2
        for record in caught:
            assert record.filename == __file__, record.filename
