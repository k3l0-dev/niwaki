"""The navigation stubs say exactly what the runtime navigator does — no more, no less.

The typed navigation classes live in ``.pyi`` stubs (``niwaki._generated_nav``)
that no runtime code ever reads, so nothing at runtime can notice a stub that
lies.  This module is that check: it parses every stub with :mod:`ast` and
confronts it, edge by edge, with the two runtime authorities:

* ``CHILD_MAP`` (``niwaki.domain._child_map``) — **which** edges exist under
  each parent class;
* the runtime navigator — **how** each edge is called.  The navigator's
  signature is read with ``inspect.signature(getattr(NiwakiNode(None, dn,
  cls), name))``: navigation computes DNs locally, so no session is needed.

The stub layout this module pins
--------------------------------

``src/niwaki/_generated_nav/`` holds ``.pyi`` files only:

* ``_base.pyi`` — ``NotANavigationName`` (``@final``, not callable), the
  ``_Sealed`` mixin (``__getattr__ -> NotANavigationName``), the universal-edge
  mixins, and the generic ``NavLeaf[T]``/``NavEnd[T]`` (+ ``Async`` twins);
* ``_root.pyi`` — ``RootNav``/``AsyncRootNav`` (the client's checker-only base:
  **not** sealed, so ``aci.tenantt`` stays an attribute error) and
  ``RootNode``/``AsyncRootNode`` (``_Sealed, RootNav, NiwakiNode[ManagedObject]``);
* ``<pkg>.pyi`` — one per ACI class package, holding ``<Cls>Node`` and
  ``Async<Cls>Node`` for every generated class of that package with at least
  one specific (non-universal) edge;
* ``__init__.pyi`` — explicit re-exports (``X as X``) of the root and base
  names.

The rules each edge must follow (the runtime shape decides, never the stub):

==================  ======================================  ========================
runtime signature   stub                                    returns
==================  ======================================  ========================
``()``              one plain ``def e(self)``               node
``(target_name="",  one plain ``def e(self, target_name:    node
**kwargs)``         str = "", **kwargs: object)``
``(name="")``       ``@overload (self, name: Literal[""]    ``Query[C]`` then node
                    = "")`` then ``@overload (self, name:
                    str)``
``(**kwargs)``      ``@overload (self)`` then keyword-only  ``Query[C]`` then node
                    overloads over the child's naming
                    props, **Python names only** (a wire
                    alias is never a parameter), types
                    from ``generate_design._param_type``
==================  ======================================  ========================

For a kwargs edge with at least one required naming prop there is exactly one
node overload whose required parameters are the model's required props.  When
every naming prop is optional, an empty call must still mean ``Query``, so
there is one node overload per prop, with that prop (and only it) required.

"node" means ``<Child>Node`` when the child has a typed class,
``NavLeaf[Child]`` when the child's only edges are the universal ones, and
``NavEnd[Child]`` when it has none — ``Async``-prefixed in the async flavour,
where ``Query`` is ``AsyncQuery`` too.

Every sealed class (``_Sealed`` among its bases — always the **first** base,
or the ``__getattr__ -> Any`` of the node would win by MRO) must expose
**every** edge of its class: a sealed class missing an edge turns valid code
into a false positive.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

import niwaki
from niwaki._codegen.generate_design import _param_type
from niwaki._codegen.generate_domain import _node_api_names, _root_api_names
from niwaki.domain._child_map import CHILD_MAP, CLASS_PKG, NAV_DEPRECATED
from niwaki.facade import AsyncNiwakiNode, NiwakiNode
from niwaki.models.base import ManagedObject

if TYPE_CHECKING:
    from niwaki.facade import AsyncNiwaki, Niwaki

NAV_DIR = Path(niwaki.__file__).resolve().parent / "_generated_nav"

# The three edges every generated class carries (a mixin in the stubs).
UNIVERSAL: dict[str, str] = {
    "annotation": "tagAnnotation",
    "annotation_to_capture_rbac_info": "aaaRbacAnnotation",
    "tag": "tagTag",
}

FLAVOURS = ("sync", "async")


# ── Runtime authorities ───────────────────────────────────────────────────────


def _row(cls: str) -> dict[str, str]:
    return dict(CHILD_MAP.get(cls, {}))


def _specific(cls: str) -> dict[str, str]:
    return {name: child for name, child in _row(cls).items() if name not in UNIVERSAL}


@cache
def _typed_parents() -> frozenset[str]:
    """Generated classes with at least one specific edge: each gets a typed class."""
    return frozenset(cls for cls in CHILD_MAP if cls in CLASS_PKG and _specific(cls))


@cache
def _model(cls: str) -> type[ManagedObject]:
    module = importlib.import_module(f"niwaki.models._generated.{CLASS_PKG[cls]}.{cls}")
    return cast(type[ManagedObject], getattr(module, cls))


@cache
def _reserved(parent: str) -> frozenset[str]:
    """Names N0 refuses in a row — the node (or client) API shadows them."""
    return _root_api_names() if parent == "_root" else _node_api_names()


def _expected_edges(parent: str) -> set[str]:
    return set(_row(parent)) - _reserved(parent)


def _runtime_navigator(parent: str, edge: str, flavour: str) -> object:
    """The navigator the runtime builds for ``<parent node>.<edge>`` — no session."""
    cls = ManagedObject if parent == "_root" else _model(parent)
    dn = "uni" if parent == "_root" else "uni/x"
    if flavour == "sync":
        return getattr(NiwakiNode(cast("Niwaki", None), dn, cls), edge)
    return getattr(AsyncNiwakiNode(cast("AsyncNiwaki", None), dn, cls), edge)


def _runtime_shape(parent: str, edge: str, flavour: str) -> str:
    """Classify the runtime navigator signature: singleton, rs, name or kwargs."""
    navigator = _runtime_navigator(parent, edge, flavour)
    assert callable(navigator)
    params = list(inspect.signature(navigator).parameters.values())
    kinds = [(p.name, p.kind, p.default) for p in params]
    pok, var_kw = inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.VAR_KEYWORD
    match kinds:
        case []:
            return "singleton"
        case [("target_name", k1, ""), (_, k2, _)] if k1 == pok and k2 == var_kw:
            return "rs"
        case [("name", k1, "")] if k1 == pok:
            return "name"
        case [(_, k1, _)] if k1 == var_kw:
            return "kwargs"
        case _:
            raise AssertionError(f"unknown runtime navigator shape for {parent}.{edge}: {kinds}")


def _node_return(child: str, flavour: str) -> str:
    pre = "Async" if flavour == "async" else ""
    if child in _typed_parents():
        return f"{pre}{child}Node"
    if _row(child):
        return f"{pre}NavLeaf[{child}]"
    return f"{pre}NavEnd[{child}]"


def _query_return(child: str, flavour: str) -> str:
    return f"{'AsyncQuery' if flavour == 'async' else 'Query'}[{child}]"


# ── Stub reader ───────────────────────────────────────────────────────────────


@dataclass
class _StubClass:
    module: str
    node: ast.ClassDef
    methods: dict[str, list[ast.FunctionDef]] = field(default_factory=dict)

    @property
    def base_names(self) -> list[str]:
        return [_bare(base) for base in self.node.bases]


def _bare(expr: ast.expr) -> str:
    """``NiwakiNode[fvBD]`` → ``NiwakiNode``; ``mod.X`` → ``X``."""
    if isinstance(expr, ast.Subscript):
        return _bare(expr.value)
    if isinstance(expr, ast.Attribute):
        return expr.attr
    if isinstance(expr, ast.Name):
        return expr.id
    return ast.unparse(expr)


@cache
def _stubs() -> dict[str, _StubClass]:
    """Every class defined in every stub, by name (names are unique across files)."""
    assert NAV_DIR.is_dir(), (
        f"{NAV_DIR} does not exist — generate the navigation stubs "
        "(uv run python -m niwaki._codegen.generate_nav)"
    )
    classes: dict[str, _StubClass] = {}
    for path in sorted(NAV_DIR.glob("*.pyi")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            assert node.name not in classes, f"{node.name} defined twice ({path.name})"
            stub = _StubClass(path.stem, node)
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    stub.methods.setdefault(item.name, []).append(item)
            classes[node.name] = stub
    assert classes, f"no class in any stub under {NAV_DIR}"
    return classes


def _mro(name: str) -> Iterator[_StubClass]:
    """The stub-defined classes of *name*'s MRO, nearest first (left-to-right DFS)."""
    seen: set[str] = set()

    def walk(current: str) -> Iterator[_StubClass]:
        stub = _stubs().get(current)
        if stub is None or current in seen:
            return
        seen.add(current)
        yield stub
        for base in stub.base_names:
            yield from walk(base)

    yield from walk(name)


def _effective_methods(name: str) -> dict[str, list[ast.FunctionDef]]:
    """Methods a checker sees on *name*: own first, then inherited (stub bases only)."""
    methods: dict[str, list[ast.FunctionDef]] = {}
    for stub in _mro(name):
        for method, defs in stub.methods.items():
            methods.setdefault(method, defs)
    return methods


def _edges(name: str) -> set[str]:
    return {m for m in _effective_methods(name) if m != "__getattr__"}


def _is_sealed(name: str) -> bool:
    return any(stub.node.name == "_Sealed" for stub in _mro(name))


def _typed_class_name(parent: str, flavour: str) -> str:
    if parent == "_root":
        return "AsyncRootNav" if flavour == "async" else "RootNav"
    return f"{'Async' if flavour == 'async' else ''}{parent}Node"


def _is_overload(func: ast.FunctionDef) -> bool:
    return any(_bare(dec) == "overload" for dec in func.decorator_list)


def _norm(expr: ast.expr | None) -> str:
    assert expr is not None, "missing annotation"
    return ast.unparse(expr)


def _norm_text(text: str) -> str:
    return ast.unparse(ast.parse(text, mode="eval").body)


# ── Shape checker ─────────────────────────────────────────────────────────────


def _check_edge(parent: str, edge: str, flavour: str) -> list[str]:
    """Every way the stub of ``parent.edge`` departs from the runtime, as messages."""
    where = f"{_typed_class_name(parent, flavour)}.{edge}"
    child = _row(parent)[edge]
    shape = _runtime_shape(parent, edge, flavour)
    defs = _effective_methods(_typed_class_name(parent, flavour))[edge]
    node_ret = _node_return(child, flavour)
    query_ret = _query_return(child, flavour)
    problems: list[str] = []

    def no_extras(func: ast.FunctionDef, *, kwarg_ok: bool = False) -> None:
        args = func.args
        if args.posonlyargs or args.vararg or (args.kwarg and not kwarg_ok):
            problems.append(f"{where}: unexpected positional-only/*args/**kwargs")

    match shape:
        case "singleton":
            if len(defs) != 1 or _is_overload(defs[0]):
                return [f"{where}: singleton must be one plain def, got {len(defs)} def(s)"]
            func = defs[0]
            no_extras(func)
            if [a.arg for a in func.args.args] != ["self"] or func.args.kwonlyargs:
                problems.append(f"{where}: singleton takes no argument")
            if _norm(func.returns) != node_ret:
                problems.append(f"{where}: returns {_norm(func.returns)}, expected {node_ret}")
        case "rs":
            if len(defs) != 1 or _is_overload(defs[0]):
                return [f"{where}: Rs-singleton must be one plain def, got {len(defs)} def(s)"]
            func = defs[0]
            no_extras(func, kwarg_ok=True)
            args = func.args
            if [a.arg for a in args.args] != ["self", "target_name"] or args.kwonlyargs:
                problems.append(f"{where}: Rs-singleton must be (self, target_name, **kwargs)")
            else:
                if _norm(args.args[1].annotation) != "str" or len(args.defaults) != 1:
                    problems.append(f"{where}: target_name must be `str` with a default")
            if args.kwarg is None or _norm(args.kwarg.annotation) != "object":
                problems.append(f"{where}: Rs-singleton must take `**kwargs: object`")
            if _norm(func.returns) != node_ret:
                problems.append(f"{where}: returns {_norm(func.returns)}, expected {node_ret}")
        case "name":
            if len(defs) != 2 or not all(map(_is_overload, defs)):
                return [f"{where}: name shape must be exactly two @overload, got {len(defs)}"]
            query, node = defs
            for func in defs:
                no_extras(func)
                if [a.arg for a in func.args.args] != ["self", "name"] or func.args.kwonlyargs:
                    problems.append(f"{where}: name overloads take (self, name)")
                    return problems
            if _norm(query.args.args[1].annotation) != _norm_text('Literal[""]') or (
                len(query.args.defaults) != 1
            ):
                problems.append(f'{where}: first overload must be `name: Literal[""] = ""`')
            if _norm(query.returns) != query_ret:
                problems.append(f"{where}: first overload returns {_norm(query.returns)}")
            if _norm(node.args.args[1].annotation) != "str" or node.args.defaults:
                problems.append(f"{where}: second overload must be `name: str`, no default")
            if _norm(node.returns) != node_ret:
                problems.append(f"{where}: second overload returns {_norm(node.returns)}")
        case "kwargs":
            problems += _check_kwargs_edge(where, child, defs, node_ret, query_ret)
        case _:  # pragma: no cover — _runtime_shape raises first
            raise AssertionError(shape)
    return problems


def _check_kwargs_edge(
    where: str, child: str, defs: list[ast.FunctionDef], node_ret: str, query_ret: str
) -> list[str]:
    model = _model(child)
    naming: list[str] = list(model._naming_props)
    fields = model.model_fields
    required = {prop for prop in naming if fields[prop].is_required()}
    if len(defs) < 2 or not all(map(_is_overload, defs)):
        return [f"{where}: kwargs shape must be @overload only (Query first), got {len(defs)}"]
    problems: list[str] = []
    query, *nodes = defs
    if [a.arg for a in query.args.args] != ["self"] or query.args.kwonlyargs:
        problems.append(f"{where}: first overload must be (self) → Query")
    if _norm(query.returns) != query_ret:
        problems.append(f"{where}: first overload returns {_norm(query.returns)}")
    required_sets: list[frozenset[str]] = []
    for func in nodes:
        args = func.args
        if [a.arg for a in args.args] != ["self"] or args.vararg or args.kwarg:
            problems.append(f"{where}: node overloads take keyword-only naming props only")
            continue
        names = [a.arg for a in args.kwonlyargs]
        if names != naming:
            problems.append(f"{where}: keyword parameters {names}, expected {naming}")
            continue
        for arg in args.kwonlyargs:
            expected_type, _enums = _param_type(fields[arg.arg].annotation)
            if _norm(arg.annotation) != _norm_text(expected_type):
                problems.append(
                    f"{where}: {arg.arg} typed {_norm(arg.annotation)}, expected {expected_type}"
                )
        required_sets.append(
            frozenset(
                arg.arg
                for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True)
                if default is None
            )
        )
        if _norm(func.returns) != node_ret:
            problems.append(f"{where}: node overload returns {_norm(func.returns)}")
    if problems:
        return problems
    # All optional: one overload per prop, so the empty call stays a Query.
    expected_sets = [frozenset(required)] if required else [frozenset({p}) for p in naming]
    if sorted(required_sets, key=sorted) != sorted(expected_sets, key=sorted):
        problems.append(
            f"{where}: required sets {[sorted(s) for s in required_sets]}, "
            f"expected {[sorted(s) for s in expected_sets]}"
        )
    return problems


# ── Preconditions on the tables (the stub rules rely on them) ─────────────────


def test_every_class_without_specific_edges_is_a_leaf_or_an_end() -> None:
    """``NavLeaf`` assumes such a class has exactly the universal edges; ``NavEnd`` none.

    A class with specific edges gets its own typed class whatever its
    universal edges are (``topRoot`` has none — its typed class must not
    inherit the universal mixin then; the edge-set test pins that).
    """
    partial = [
        cls
        for cls in CLASS_PKG
        if not _specific(cls) and _row(cls) and set(_row(cls).items()) != set(UNIVERSAL.items())
    ]
    assert not partial, f"classes neither leaf nor end: {partial[:10]}"
    assert all(_row("_root").get(n) == c for n, c in UNIVERSAL.items())


def test_no_navigation_name_is_reserved_by_the_node_api() -> None:
    """N0 refuses them at generation; the stubs therefore cover every row entry."""
    clashes = [
        (parent, name)
        for parent in (*_typed_parents(), "_root")
        for name in _row(parent)
        if name in _reserved(parent)
    ]
    assert not clashes


def test_no_deprecated_navigation_name_to_emit() -> None:
    """The stubs emit no deprecated alias today.

    When ``NAV_DEPRECATED`` gains a row, the stubs must emit it as a
    ``@deprecated`` (PEP 702) edge and this module must pin that — this test
    turns red to say so.
    """
    assert not any(NAV_DEPRECATED.values())


# ── Layout ────────────────────────────────────────────────────────────────────


def test_stub_files_are_exactly_base_root_init_and_one_per_package() -> None:
    expected = {"__init__.pyi", "_base.pyi", "_root.pyi"} | {
        f"{CLASS_PKG[cls]}.pyi" for cls in _typed_parents()
    }
    actual = {path.name for path in NAV_DIR.iterdir() if path.suffix in {".py", ".pyi"}}
    assert actual == expected, (
        f"missing: {sorted(expected - actual)[:5]}, extra: {sorted(actual - expected)[:5]}"
    )


def test_every_typed_parent_has_both_flavours_in_its_package_module() -> None:
    stubs = _stubs()
    missing: list[str] = []
    misplaced: list[str] = []
    for parent in sorted(_typed_parents()):
        for flavour in FLAVOURS:
            name = _typed_class_name(parent, flavour)
            if name not in stubs:
                missing.append(name)
            elif stubs[name].module != CLASS_PKG[parent]:
                misplaced.append(f"{name} in {stubs[name].module}.pyi")
    assert not missing, f"{len(missing)} typed classes missing: {missing[:10]}"
    assert not misplaced, f"typed classes outside their package module: {misplaced[:10]}"
    assert len(_typed_parents()) > 800


def test_no_typed_class_for_a_class_without_specific_edges() -> None:
    node_classes = {
        name
        for name, stub in _stubs().items()
        if name.endswith("Node") and stub.module not in {"_base", "_root"}
    }
    expected = {_typed_class_name(p, f) for p in _typed_parents() for f in FLAVOURS}
    assert node_classes == expected, sorted(node_classes ^ expected)[:10]


def test_init_reexports_the_public_stub_names_explicitly() -> None:
    tree = ast.parse((NAV_DIR / "__init__.pyi").read_text(encoding="utf-8"))
    exported = {
        alias.asname
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.asname == alias.name
    }
    for name in ("RootNav", "RootNode", "NavLeaf", "NavEnd", "NotANavigationName"):
        assert name in exported, f"__init__.pyi must re-export {name} as {name}"
        assert f"Async{name}" in exported or name == "NotANavigationName"


# ── Sealing ───────────────────────────────────────────────────────────────────


def test_sentinel_is_final_and_not_callable() -> None:
    stubs = _stubs()
    sentinel = stubs["NotANavigationName"]
    assert any(_bare(dec) == "final" for dec in sentinel.node.decorator_list)
    assert "__call__" not in _effective_methods("NotANavigationName")
    sealed = _effective_methods("_Sealed")["__getattr__"]
    assert len(sealed) == 1 and _norm(sealed[0].returns) == "NotANavigationName"


def test_sealed_mixin_is_always_the_first_base() -> None:
    """After ``NiwakiNode[T]`` the node's ``__getattr__ -> Any`` would win by MRO."""
    wrong = [
        name
        for name, stub in _stubs().items()
        if "_Sealed" in stub.base_names and stub.base_names[0] != "_Sealed"
    ]
    assert not wrong, wrong[:10]


def test_every_node_family_is_sealed_and_the_client_base_is_not() -> None:
    stubs = _stubs()
    nodes = [name for name in stubs if name.endswith("Node")]
    generic = ["NavLeaf", "NavEnd", "AsyncNavLeaf", "AsyncNavEnd"]
    unsealed = [name for name in (*nodes, *generic) if not _is_sealed(name)]
    assert not unsealed, unsealed[:10]
    # The client derives from RootNav: sealing it would turn `aci.tenantt`
    # into a call error instead of an attribute error, and hide real API typos.
    assert not _is_sealed("RootNav")
    assert not _is_sealed("AsyncRootNav")


def test_node_classes_derive_from_the_node_of_their_model() -> None:
    """``read()`` returns the concrete model because the base is ``NiwakiNode[<Cls>]``."""
    stubs = _stubs()
    wrong: list[str] = []
    for parent in _typed_parents():
        for flavour, node_base in (("sync", "NiwakiNode"), ("async", "AsyncNiwakiNode")):
            name = _typed_class_name(parent, flavour)
            last = stubs[name].node.bases[-1] if name in stubs else None
            if last is None or ast.unparse(last) != f"{node_base}[{parent}]":
                wrong.append(name)
    for name, expected in (
        ("RootNode", "NiwakiNode[ManagedObject]"),
        ("AsyncRootNode", "AsyncNiwakiNode[ManagedObject]"),
    ):
        if ast.unparse(stubs[name].node.bases[-1]) != expected:
            wrong.append(name)
        root_nav = "AsyncRootNav" if name.startswith("Async") else "RootNav"
        if root_nav not in stubs[name].base_names:
            wrong.append(f"{name} (no {root_nav} base)")
    assert not wrong, wrong[:10]


def test_no_sealed_class_misses_an_edge_of_its_class() -> None:
    """Pitfall 9: a partially sealed parent turns valid navigation into an error."""
    missing: list[str] = []
    for parent in sorted(_typed_parents()):
        for flavour in FLAVOURS:
            name = _typed_class_name(parent, flavour)
            lost = _expected_edges(parent) - _edges(name)
            if lost:
                missing.append(f"{name}: {sorted(lost)}")
    for name in ("RootNode", "AsyncRootNode"):
        lost = _expected_edges("_root") - _edges(name)
        if lost:
            missing.append(f"{name}: {sorted(lost)}")
    assert not missing, f"{len(missing)} sealed classes miss edges: {missing[:5]}"


def test_leaf_and_end_expose_exactly_the_edges_of_their_children() -> None:
    """``NavLeaf[C]`` has the universal edges, ``NavEnd[C]`` none — and C agrees."""
    for pre in ("", "Async"):
        assert _edges(f"{pre}NavLeaf") == set(UNIVERSAL)
        assert _edges(f"{pre}NavEnd") == set()
    leaves: set[str] = set()
    ends: set[str] = set()
    for stub in _stubs().values():
        for defs in stub.methods.values():
            for func in defs:
                returned = func.returns
                if isinstance(returned, ast.Subscript):
                    family, child = _bare(returned.value), ast.unparse(returned.slice)
                    if family in {"NavLeaf", "AsyncNavLeaf"}:
                        leaves.add(child)
                    elif family in {"NavEnd", "AsyncNavEnd"}:
                        ends.add(child)
    assert leaves and ends
    wrong_leaves = [c for c in leaves if set(_row(c)) != set(UNIVERSAL)]
    wrong_ends = [c for c in ends if _row(c)]
    assert not wrong_leaves, f"NavLeaf over classes with specific edges: {wrong_leaves[:5]}"
    assert not wrong_ends, f"NavEnd over classes that have edges: {wrong_ends[:5]}"


# ── Edge sets and shapes against CHILD_MAP and the runtime navigator ──────────


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_edge_sets_equal_child_map(flavour: str) -> None:
    """Each typed class exposes exactly its row — universal edges via the mixin."""
    diffs: list[str] = []
    for parent in sorted((*_typed_parents(), "_root")):
        name = _typed_class_name(parent, flavour)
        expected, actual = _expected_edges(parent), _edges(name)
        if expected != actual:
            diffs.append(
                f"{name}: missing {sorted(expected - actual)[:3]}, "
                f"extra {sorted(actual - expected)[:3]}"
            )
    assert not diffs, f"{len(diffs)} classes diverge from CHILD_MAP: {diffs[:5]}"


def test_root_node_exposes_the_root_row() -> None:
    for name in ("RootNode", "AsyncRootNode"):
        assert _edges(name) == _expected_edges("_root")


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_edge_shapes_match_the_runtime_navigator(flavour: str) -> None:
    """Name, parameters, overloads and return of every edge follow the runtime."""
    problems: list[str] = []
    checked = 0
    for parent in sorted((*_typed_parents(), "_root")):
        name = _typed_class_name(parent, flavour)
        for edge in sorted(_expected_edges(parent) & _edges(name)):
            problems += _check_edge(parent, edge, flavour)
            checked += 1
    assert checked > 5_000, f"only {checked} edges checked — the walk is broken"
    assert not problems, f"{len(problems)} edge(s) diverge:\n" + "\n".join(problems[:15])


def test_sync_and_async_navigators_share_one_signature() -> None:
    """The conformance reads one runtime authority for both flavours."""
    for parent, edge in (
        ("fvBD", "subnet"),
        ("fvBD", "vrf_binding"),
        ("fvTenant", "bd"),
        ("fvBD", "pim"),
        ("_root", "vmm_provider"),
    ):
        sync, async_ = (_runtime_navigator(parent, edge, f) for f in FLAVOURS)
        assert callable(sync) and callable(async_)
        assert str(inspect.signature(sync)) == str(inspect.signature(async_))


def test_runtime_shapes_cover_all_four_families() -> None:
    """Sanity: the classifier sees every family on known edges."""
    assert _runtime_shape("fvBD", "pim", "sync") == "singleton"
    assert _runtime_shape("fvBD", "vrf_binding", "sync") == "rs"
    assert _runtime_shape("fvTenant", "bd", "sync") == "name"
    assert _runtime_shape("fvBD", "subnet", "sync") == "kwargs"
    assert _runtime_shape("_root", "tenant", "sync") == "name"


# ── Wire aliases never reach the typed surface ────────────────────────────────


def test_no_stub_parameter_is_a_wire_alias() -> None:
    """Keyword parameters are Python field names; a wire alias is never declared."""
    offenders: list[str] = []
    for parent in sorted((*_typed_parents(), "_root")):
        name = _typed_class_name(parent, "sync")
        methods = _effective_methods(name)
        for edge in sorted(_expected_edges(parent) & set(methods)):
            fields = _model(_row(parent)[edge]).model_fields
            for func in methods[edge]:
                for arg in func.args.kwonlyargs:
                    if arg.arg not in fields:
                        offenders.append(f"{name}.{edge}({arg.arg}=)")
    assert not offenders, offenders[:10]
    # The canonical case: fvSubnet is named by `subnet` (wire `ip`).
    subnet = _effective_methods("fvBDNode")["subnet"]
    assert ["subnet"] in [[a.arg for a in f.args.kwonlyargs] for f in subnet]
    assert not any(a.arg == "ip" for f in subnet for a in f.args.kwonlyargs)


# ── File hygiene ──────────────────────────────────────────────────────────────


def _leading_comments(text: str) -> list[str]:
    lines: list[str] = []
    for line in text.splitlines():
        if not line.startswith("#"):
            break
        lines.append(line)
    return lines


def test_every_stub_carries_the_generated_banner() -> None:
    bad = [
        path.name
        for path in sorted(NAV_DIR.glob("*.pyi"))
        if not (
            "Generated by" in (text := path.read_text(encoding="utf-8"))
            and "do not edit manually" in text
            and text.endswith("\n")
        )
    ]
    assert not bad, bad[:10]


def test_overloading_stubs_disable_overload_overlap_in_their_header() -> None:
    """``Literal[""]`` vs ``str`` overlaps by design; mypy is told so per file."""
    header = '# mypy: disable-error-code="overload-overlap"'
    bad = [
        path.name
        for path in sorted(NAV_DIR.glob("*.pyi"))
        if "@overload" in (text := path.read_text(encoding="utf-8"))
        and header not in _leading_comments(text)
    ]
    assert not bad, bad[:10]


def test_stubs_never_use_any() -> None:
    """Typing is strict end to end: no ``Any`` anywhere in the stubs."""
    offenders = [
        path.name
        for path in sorted(NAV_DIR.glob("*.pyi"))
        if any(
            (isinstance(node, ast.Name) and node.id == "Any")
            or (isinstance(node, ast.alias) and node.name == "Any")
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    ]
    assert not offenders, offenders
