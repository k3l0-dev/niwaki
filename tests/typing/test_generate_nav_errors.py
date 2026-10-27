"""The navigation stub generator refuses what it cannot describe faithfully.

``niwaki._codegen.generate_nav`` turns ``CHILD_MAP`` into ``.pyi`` stubs that no
runtime code ever reads, so a stub that lies would go unnoticed.  The generator
therefore raises :class:`~niwaki._codegen.generate_nav.NavGenerationError`
rather than emit one.  This module drives every refusal path, and the edge
cases next to them, with synthetic inputs:

* a **tiny synthetic table** (tenant → bd → leaf/Rs/kwargs children) patched
  over the module's ``CHILD_MAP`` — the generator's real code runs end to end
  (``render_nav`` included, ruff pass and all) in well under a second;
* the generator's internal seams (``_Imports``, ``_module_text``,
  ``_node_return``, ``_edge_lines``, ``_ruff``) for the paths a consistent
  table cannot reach.

The generator caches its table-derived views (``_universal_row``,
``_typed_parents``, ``_model``, ``_reserved``); the fixture clears them before
and after each test so a synthetic table never leaks into another test.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from niwaki._codegen import generate_nav as gn
from niwaki._codegen.generate_nav import NavGenerationError
from niwaki.models._generated.fv.fvSubnet import fvSubnet

_UNIVERSAL: dict[str, str] = {
    "annotation": "tagAnnotation",
    "annotation_to_capture_rbac_info": "aaaRbacAnnotation",
    "tag": "tagTag",
}


def _tiny_table() -> dict[str, dict[str, str]]:
    """A consistent miniature ``CHILD_MAP`` over real generated classes."""
    return {
        "_root": {**_UNIVERSAL, "tenant": "fvTenant"},
        "fvTenant": {**_UNIVERSAL, "bd": "fvBD"},
        "fvBD": {
            **_UNIVERSAL,
            "legacy_mode": "fvAccP",  # singleton → NavLeaf
            "subnet": "fvSubnet",  # kwargs (wire alias `ip`)
            "vrf_binding": "fvRsCtx",  # Rs-singleton
        },
        "fvAccP": dict(_UNIVERSAL),
        "fvSubnet": dict(_UNIVERSAL),
        "fvRsCtx": dict(_UNIVERSAL),
    }


def _clear_caches() -> None:
    for cached in (gn._universal_row, gn._typed_parents, gn._model, gn._reserved):
        cached.cache_clear()


@pytest.fixture
def table(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, dict[str, str]]]:
    """Patch a mutable tiny table over the generator; mutate it before rendering."""
    child_map = _tiny_table()
    monkeypatch.setattr(gn, "CHILD_MAP", child_map)
    _clear_caches()
    yield child_map
    _clear_caches()


def _render(table: Mapping[str, Mapping[str, str]]) -> dict[Path, str]:
    """Render with the patched table (caches cleared after the last mutation)."""
    _clear_caches()
    return gn.render_nav()


# ── Nominal: the synthetic table renders (proves the harness is sound) ───────


def test_tiny_table_renders_the_expected_modules(table: dict[str, dict[str, str]]) -> None:
    rendered = _render(table)
    assert set(rendered) == {
        Path("__init__.pyi"),
        Path("_base.pyi"),
        Path("_root.pyi"),
        Path("fv.pyi"),
    }
    fv = rendered[Path("fv.pyi")]
    assert "class fvBDNode(_Sealed, _UniversalNav, NiwakiNode[fvBD]):" in fv
    assert "def legacy_mode(self) -> NavLeaf[fvAccP]: ..." in fv
    assert 'def vrf_binding(self, target_name: str = "", **kwargs: object) -> NavLeaf' in fv
    assert "def subnet(self, *, subnet: str) -> NavLeaf[fvSubnet]: ..." in fv
    assert "ip:" not in fv  # the wire alias never becomes a parameter


# ── Edge cases ────────────────────────────────────────────────────────────────


def test_child_with_an_empty_row_is_a_nav_end(table: dict[str, dict[str, str]]) -> None:
    """A child whose ``CHILD_MAP`` row is empty (or absent) has no edge: ``NavEnd``."""
    table["fvAccP"] = {}
    del table["fvSubnet"]
    fv = _render(table)[Path("fv.pyi")]
    assert "def legacy_mode(self) -> NavEnd[fvAccP]: ..." in fv
    assert "def subnet(self, *, subnet: str) -> NavEnd[fvSubnet]: ..." in fv
    assert "def legacy_mode(self) -> AsyncNavEnd[fvAccP]: ..." in fv


def test_parent_with_an_empty_row_gets_no_typed_class(table: dict[str, dict[str, str]]) -> None:
    table["fvBD"] = {}
    fv = _render(table)[Path("fv.pyi")]
    assert "class fvBDNode" not in fv
    assert "def bd(self, name: str) -> NavEnd[fvBD]: ..." in fv


def test_parent_whose_only_specific_edge_is_refused_by_n0(
    table: dict[str, dict[str, str]],
) -> None:
    """A name the node API shadows is never reached at runtime, so never declared.

    The parent still has a specific edge (so it stays a typed, sealed class)
    but declares nothing itself: its body holds only the universal mixin.
    """
    table["fvBD"] = {**_UNIVERSAL, "read": "fvSubnet"}
    fv = _render(table)[Path("fv.pyi")]
    assert "class fvBDNode(_Sealed, _UniversalNav, NiwakiNode[fvBD]):" in fv
    assert "def read(" not in fv
    body = fv.split("class fvBDNode(", 1)[1].split("\nclass ", 1)[0]
    assert "def " not in body


def test_root_names_shadowed_by_the_client_api_are_never_declared(
    table: dict[str, dict[str, str]],
) -> None:
    """In ``_root`` the client API is reserved too (``aci.query`` is a method)."""
    table["_root"] = {**table["_root"], "query": "fvTenant", "close": "fvTenant"}
    root = _render(table)[Path("_root.pyi")]
    assert "def query(" not in root
    assert "def close(" not in root
    assert "def tenant(" in root


def test_keyword_and_underscore_names_are_never_declared(
    table: dict[str, dict[str, str]],
) -> None:
    """Unreachable through ``__getattr__`` at runtime — and illegal as method names."""
    table["fvBD"] = {**table["fvBD"], "class": "fvAccP", "_hidden": "fvAccP"}
    fv = _render(table)[Path("fv.pyi")]
    assert "def class(" not in fv
    assert "def _hidden(" not in fv
    assert "def legacy_mode(" in fv


def test_parent_without_universal_edges_gets_no_universal_mixin(
    table: dict[str, dict[str, str]],
) -> None:
    """Like ``topRoot``: specific edges only — sealed, but no universal mixin."""
    table["fvBD"] = {"legacy_mode": "fvAccP"}
    fv = _render(table)[Path("fv.pyi")]
    assert "class fvBDNode(_Sealed, NiwakiNode[fvBD]):" in fv


# ── Refusals: tables the stubs cannot describe ────────────────────────────────


def test_naming_prop_that_is_not_a_model_field(
    table: dict[str, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A naming prop missing from ``model_fields`` would emit a parameter nothing reads."""
    monkeypatch.setattr(fvSubnet, "_naming_props", ["subnet", "ghost"])
    with pytest.raises(NavGenerationError, match=r"fvSubnet: naming props \['ghost'\]"):
        gn._edge_lines("subnet", "fvSubnet", gn._SYNC, gn._Imports("fv"))
    with pytest.raises(NavGenerationError, match="ghost"):
        _render(table)


@pytest.mark.parametrize(
    "leaf_row",
    [
        pytest.param({"tag": "tagTag"}, id="partial-universal"),
        pytest.param({**_UNIVERSAL, "tag": "tagAnnotation"}, id="universal-name-wrong-child"),
    ],
)
def test_leaf_whose_edges_are_not_exactly_the_universal_set(
    table: dict[str, dict[str, str]], leaf_row: dict[str, str]
) -> None:
    """Neither a leaf nor an end: ``NavLeaf`` would claim edges it does not have."""
    table["fvAccP"] = leaf_row
    _clear_caches()
    with pytest.raises(NavGenerationError, match="neither specific nor exactly the universal"):
        gn._node_return("fvAccP", gn._SYNC, gn._Imports("fv"))
    with pytest.raises(NavGenerationError, match="fvAccP"):
        _render(table)


def test_universal_edges_plus_a_specific_one_make_a_typed_parent(
    table: dict[str, dict[str, str]],
) -> None:
    """Not a refusal: one non-universal name turns the would-be leaf into a parent."""
    table["fvAccP"] = {**_UNIVERSAL, "extra": "tagTag"}
    fv = _render(table)[Path("fv.pyi")]
    assert "class fvAccPNode(_Sealed, _UniversalNav, NiwakiNode[fvAccP]):" in fv
    assert "def legacy_mode(self) -> fvAccPNode: ..." in fv


def test_root_row_without_the_universal_edges_is_refused_by_the_guard(
    table: dict[str, dict[str, str]],
) -> None:
    del table["_root"]["annotation_to_capture_rbac_info"]
    _clear_caches()
    with pytest.raises(NavGenerationError, match="annotation_to_capture_rbac_info"):
        gn._universal_row()


def test_root_row_without_the_universal_edges_is_refused_by_render_nav(
    table: dict[str, dict[str, str]],
) -> None:
    del table["_root"]["annotation_to_capture_rbac_info"]
    with pytest.raises(NavGenerationError, match="annotation_to_capture_rbac_info"):
        _render(table)


def test_edge_to_a_class_that_is_not_generated(table: dict[str, dict[str, str]]) -> None:
    table["fvBD"] = {**table["fvBD"], "operational": "topSystem"}
    with pytest.raises(NavGenerationError, match="topSystem is a navigation target"):
        _render(table)


def test_aci_package_named_like_a_stub_module(
    table: dict[str, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A package called ``_base`` would overwrite the shared base stub."""
    monkeypatch.setattr(gn, "CLASS_PKG", {**gn.CLASS_PKG, "fvBD": "_base"})
    with pytest.raises(NavGenerationError, match="collides with a stub module name"):
        _render(table)


# ── Refusals: name clashes inside one stub module ─────────────────────────────


def test_one_name_imported_from_two_modules() -> None:
    imports = gn._Imports("fv")
    imports.add("niwaki.facade", "NiwakiNode")
    imports.add("niwaki.facade", "NiwakiNode")  # the same binding twice is fine
    with pytest.raises(NavGenerationError, match="'NiwakiNode' imported from both"):
        imports.add("somewhere.else", "NiwakiNode")


def test_names_of_the_module_itself_are_never_imported() -> None:
    imports = gn._Imports("fv")
    imports.nav("fv", "fvBDNode")
    assert imports.names == {}
    imports.nav("_base", "_Sealed")
    assert imports.names == {"_Sealed": "niwaki._generated_nav._base"}


def test_class_defined_under_an_imported_name() -> None:
    imports = gn._Imports("fv")
    imports.add("niwaki.models._generated.fv.fvBD", "fvBD")
    with pytest.raises(NavGenerationError, match=r"defines \['fvBD'\], also imported"):
        gn._module_text(imports, ["class fvBD: ..."], ["fvBD"])


def test_module_text_without_clash_carries_banner_and_overload_header() -> None:
    imports = gn._Imports("fv")
    imports.add("typing", "overload")
    text = gn._module_text(
        imports, ["class X:", "    @overload", "    def f(self) -> int: ..."], ["X"]
    )
    assert text.startswith("#") and "do not edit manually" in text
    assert '# mypy: disable-error-code="overload-overlap"' in text
    plain = gn._module_text(gn._Imports("fv"), ["class Y: ..."], ["Y"])
    assert "overload-overlap" not in plain


# ── Refusals: the ruff pass ───────────────────────────────────────────────────


def test_ruff_refuses_unparseable_text() -> None:
    """The real ruff, on text it cannot format: a loud error, not a silent stub."""
    with pytest.raises(NavGenerationError, match="failed on the navigation stubs"):
        gn._ruff({Path("broken.pyi"): "def (:\n"})


@pytest.mark.parametrize("failing_call", [0, 1, 2], ids=["format", "check-fix", "reformat"])
def test_ruff_subprocess_failure_at_every_step(
    monkeypatch: pytest.MonkeyPatch, failing_call: int
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        code = 2 if len(calls) - 1 == failing_call else 0
        return subprocess.CompletedProcess(command, code, stdout="ruff said no", stderr="")

    monkeypatch.setattr(gn.subprocess, "run", fake_run)
    with pytest.raises(NavGenerationError, match="ruff said no"):
        gn._ruff({Path("x.pyi"): "class X: ...\n"})
    assert len(calls) == failing_call + 1, "ruff must stop at the first failing step"


def test_ruff_success_returns_every_input() -> None:
    texts = {Path("a.pyi"): "class A: ...\n", Path("b.pyi"): "class B: ...\n"}
    assert set(gn._ruff(texts)) == set(texts)


# ── write_nav: a refusal writes nothing ───────────────────────────────────────


def test_write_nav_propagates_a_refusal_and_leaves_the_directory_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nav = tmp_path / "_generated_nav"
    nav.mkdir()
    existing = nav / "fv.pyi"
    existing.write_text("# committed\n", encoding="utf-8")

    def refuse() -> dict[Path, str]:
        raise NavGenerationError("synthetic refusal")

    monkeypatch.setattr(gn, "render_nav", refuse)
    with pytest.raises(NavGenerationError, match="synthetic refusal"):
        gn.write_nav(nav)
    assert existing.read_text(encoding="utf-8") == "# committed\n"
    assert sorted(p.name for p in nav.iterdir()) == ["fv.pyi"]


def test_write_nav_refusal_does_not_create_a_missing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse() -> dict[Path, str]:
        raise NavGenerationError("synthetic refusal")

    monkeypatch.setattr(gn, "render_nav", refuse)
    with pytest.raises(NavGenerationError):
        gn.write_nav(tmp_path / "absent")
    assert not (tmp_path / "absent").exists()


def test_the_error_is_a_value_error() -> None:
    """Callers catching ``ValueError`` (the generators' convention) still catch it."""
    assert issubclass(NavGenerationError, ValueError)
