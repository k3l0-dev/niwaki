"""Generate ``src/niwaki/_generated_nav/`` — typed façade navigation stubs.

Façade navigation (``aci.tenant("prod").bd("web").subnet(subnet=...)``) is
resolved at runtime by ``__getattr__`` against ``CHILD_MAP``, which a type
checker cannot follow: without help it sees ``Any`` all the way down.  This
generator emits **stubs** (``.pyi`` files, never a ``.py``) that describe that
navigation edge by edge, so mypy and pyright see a typed class per navigation
node — completion, concrete ``read()`` types and typo detection — while the
runtime keeps resolving every name through ``__getattr__``, unchanged.

The package is *stub-only*: with no ``.py`` module in it, nothing can import
it at runtime.  :mod:`niwaki.facade` wires it in under ``TYPE_CHECKING`` only.

Usage (from repo root)::

    uv run python -m niwaki._codegen.generate_nav

Inputs (all committed — the generator is corpus-free):

- ``niwaki.domain._child_map`` — ``CHILD_MAP`` (which edges exist under each
  parent), ``CLASS_PKG`` (each class's package) and ``RS_TARGET_PROP`` (the Rs
  singletons that take a target name);
- the generated models — their naming props and field annotations, rendered
  through :func:`niwaki._codegen.generate_design._param_type`, the same type
  rendering the design cursors use;
- the façade's own API (:func:`niwaki._codegen.generate_domain._node_api_names`
  and :func:`~niwaki._codegen.generate_domain._root_api_names`): a name the
  node or the client already carries is never a navigation edge.

Layout of the output
--------------------

``_base.pyi``
    ``NotANavigationName`` (``@final``, not callable — what an unknown
    attribute of a typed node resolves to, so a typo *called* is an error),
    the ``_Sealed`` mixin (``__getattr__ -> NotANavigationName``), the
    universal-edge mixins (``tag``/``annotation``/
    ``annotation_to_capture_rbac_info``, carried by every generated class) and
    the generic ``NavLeaf[T]`` (universal edges only) and ``NavEnd[T]`` (no
    edge at all), each with its ``Async`` twin.
``_root.pyi``
    ``RootNav``/``AsyncRootNav`` — the client's checker-only base, **not**
    sealed (``aci.tenantt`` must stay an attribute error, and the client has
    its own API) — and ``RootNode``/``AsyncRootNode``, the typed ``aci.root``.
``<pkg>.pyi``
    One per ACI class package: ``<Cls>Node`` and ``Async<Cls>Node`` for every
    generated class of that package with at least one specific
    (non-universal) edge.
``__init__.pyi``
    Explicit re-exports (``X as X``) of the base and root names.

Each edge mirrors the runtime navigator's shape exactly (see
:func:`niwaki.facade._make_jargon_navigator`):

==========================  ==============================================
child naming props          stub
==========================  ==============================================
none, Rs target             ``(self, target_name: str = "", **kwargs:
                            object) -> node``
none                        ``(self) -> node``
``["name"]``                ``@overload (self, name: Literal[""] = "") ->
                            Query[C]`` and ``@overload (self, name: str)
                            -> node``
anything else               ``@overload (self) -> Query[C]`` then
                            keyword-only Python names (never a wire alias)
                            -> node; when every prop is optional, one
                            overload per prop with that prop required, so
                            the empty call still means ``Query``
==========================  ==============================================

"node" is ``<Child>Node`` when the child has a typed class, ``NavLeaf[Child]``
when its only edges are the universal ones, ``NavEnd[Child]`` when it has
none.  The output text is the exact committed text: the generator runs
``ruff check --fix`` and ``ruff format`` (the repo configuration) on it before
returning it, so ``regen.sh`` does not reformat it.
"""

from __future__ import annotations

import keyword
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import cache
from importlib import import_module
from pathlib import Path

from niwaki._codegen.generate_design import _naming_params
from niwaki._codegen.generate_domain import _node_api_names, _root_api_names
from niwaki.domain._child_map import CHILD_MAP, CLASS_PKG, RS_TARGET_PROP
from niwaki.models.base import ManagedObject

_SRC = Path(__file__).resolve().parent.parent  # src/niwaki
_NAV_DIR = _SRC / "_generated_nav"
_PYPROJECT = _SRC.parent.parent / "pyproject.toml"

_NAV_PACKAGE = "niwaki._generated_nav"
_ROOT_KEY = "_root"
_BASE_MODULE = "_base"
_ROOT_MODULE = "_root"

# The edges every generated class carries.  They live in one mixin per flavour
# instead of being repeated in ~1,800 classes; their children are read from the
# ``_root`` row (which carries them too).
_UNIVERSAL_NAMES: tuple[str, ...] = ("annotation", "annotation_to_capture_rbac_info", "tag")

_BANNER = """\
# Generated by niwaki generate_nav — do not edit manually.
# Re-generate: uv run python -m niwaki._codegen.generate_nav
# Typed façade navigation, for type checkers only: this package has no runtime module.
"""

# ``name: Literal[""] = ""`` (→ Query) overlaps ``name: str`` (→ node) by
# design; mypy reports that unless told otherwise, pyright does not.
_OVERLOAD_HEADER = '# mypy: disable-error-code="overload-overlap"\n'


class NavGenerationError(ValueError):
    """The navigation tables have a shape the stubs cannot describe faithfully.

    Raised instead of emitting a stub that would lie about the runtime: a
    naming prop missing from its model, a class whose edges are neither its
    own nor exactly the universal ones, or two different objects competing
    for one name in a stub module.
    """


# ── Flavours ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Flavour:
    """The names one flavour (sync or async) of the façade uses."""

    prefix: str
    node: str
    query: str
    query_module: str
    universal: str


_SYNC = _Flavour(
    prefix="",
    node="NiwakiNode",
    query="Query",
    query_module="niwaki.query._builder",
    universal="_UniversalNav",
)
_ASYNC = _Flavour(
    prefix="Async",
    node="AsyncNiwakiNode",
    query="AsyncQuery",
    query_module="niwaki.query._async_builder",
    universal="_AsyncUniversalNav",
)
_FLAVOURS: tuple[_Flavour, ...] = (_SYNC, _ASYNC)


# ── Tables ────────────────────────────────────────────────────────────────────


def _row(cls: str) -> Mapping[str, str]:
    """The navigation row of *cls* (``{name: child class}``), empty when it has none."""
    return CHILD_MAP.get(cls, {})


@cache
def _universal_row() -> dict[str, str]:
    """``{universal edge name: child class}``, read from the ``_root`` row.

    Raises:
        NavGenerationError: The ``_root`` row lacks a universal edge.
    """
    root = _row(_ROOT_KEY)
    missing = [name for name in _UNIVERSAL_NAMES if name not in root]
    if missing:
        raise NavGenerationError(f"the _root row lacks the universal edges {missing}")
    return {name: root[name] for name in _UNIVERSAL_NAMES}


def _has_universal(cls: str) -> bool:
    """Whether *cls* carries every universal edge, to the universal children."""
    row = _row(cls)
    return all(row.get(name) == child for name, child in _universal_row().items())


def _specific(cls: str) -> dict[str, str]:
    """The edges of *cls* other than the universal names."""
    return {name: child for name, child in _row(cls).items() if name not in _UNIVERSAL_NAMES}


@cache
def _typed_parents() -> frozenset[str]:
    """Generated classes with at least one specific edge: each gets a typed class."""
    return frozenset(cls for cls in CHILD_MAP if cls in CLASS_PKG and _specific(cls))


@cache
def _model(cls: str) -> type[ManagedObject]:
    """Import the generated model class *cls* (canonical path).

    Raises:
        NavGenerationError: *cls* is not a generated class.
    """
    pkg = CLASS_PKG.get(cls)
    if pkg is None:
        raise NavGenerationError(f"{cls} is a navigation target but not a generated class")
    model: object = getattr(import_module(f"niwaki.models._generated.{pkg}.{cls}"), cls)
    if not (isinstance(model, type) and issubclass(model, ManagedObject)):
        raise NavGenerationError(f"niwaki.models._generated.{pkg}.{cls} is not a model class")
    return model


@cache
def _reserved(parent: str) -> frozenset[str]:
    """Names that can never be an edge under *parent*.

    The node (or, for ``_root``, the client) API shadows them, so the runtime
    never reaches ``__getattr__`` for them; ``generate_domain`` refuses them in
    the table (N0), and the stubs skip them for the same reason.
    """
    return _root_api_names() if parent == _ROOT_KEY else _node_api_names()


def _edges_of(parent: str, names: Iterable[str]) -> list[tuple[str, str]]:
    """``(name, child)`` for each reachable edge of *parent* among *names*, sorted.

    A name the API shadows, a Python keyword, or an underscore name is never
    reached through ``__getattr__`` at runtime, so it is never declared.
    """
    row = _row(parent)
    reserved = _reserved(parent)
    return [
        (name, row[name])
        for name in sorted(names)
        if name not in reserved and not keyword.iskeyword(name) and not name.startswith("_")
    ]


# ── Imports of one stub module ────────────────────────────────────────────────


@dataclass
class _Imports:
    """The ``from module import name`` lines one stub module needs.

    Attributes:
        here: The stub module being rendered (``"fv"``, ``"_base"``, ...);
            names it defines itself are never imported.
        names: ``{bound name: module}``.
    """

    here: str
    names: dict[str, str] = field(default_factory=dict[str, str])

    def add(self, module: str, name: str) -> None:
        """Import *name* from *module*.

        Raises:
            NavGenerationError: *name* is already bound to another module.
        """
        if module == f"{_NAV_PACKAGE}.{self.here}":
            return
        bound = self.names.setdefault(name, module)
        if bound != module:
            raise NavGenerationError(
                f"{_NAV_PACKAGE}.{self.here}: {name!r} imported from both {bound} and {module}"
            )

    def model(self, cls: str) -> None:
        """Import the generated model class *cls*."""
        self.add(f"niwaki.models._generated.{CLASS_PKG[cls]}.{cls}", cls)

    def nav(self, module: str, name: str) -> None:
        """Import *name* from the stub module *module* (a no-op inside it)."""
        self.add(f"{_NAV_PACKAGE}.{module}", name)

    def render(self) -> str:
        """The import block; ``ruff check --fix`` sorts it afterwards."""
        by_module: dict[str, list[str]] = {}
        for name, module in self.names.items():
            by_module.setdefault(module, []).append(name)
        return "".join(
            f"from {module} import {', '.join(sorted(names))}\n"
            for module, names in sorted(by_module.items())
        )


# ── Edges ─────────────────────────────────────────────────────────────────────


def _node_return(child: str, flavour: _Flavour, imports: _Imports) -> str:
    """The node type a navigation to *child* returns, importing what it names.

    Raises:
        NavGenerationError: *child* has edges, none specific, and they are not
            exactly the universal ones — neither a leaf nor an end.
    """
    imports.model(child)
    if child in _typed_parents():
        name = f"{flavour.prefix}{child}Node"
        imports.nav(CLASS_PKG[child], name)
        return name
    if _row(child):
        if dict(_row(child)) != _universal_row():
            raise NavGenerationError(
                f"{child} has edges {sorted(_row(child))} that are neither specific "
                "nor exactly the universal ones"
            )
        family = f"{flavour.prefix}NavLeaf"
    else:
        family = f"{flavour.prefix}NavEnd"
    imports.nav(_BASE_MODULE, family)
    return f"{family}[{child}]"


def _edge_lines(name: str, child: str, flavour: _Flavour, imports: _Imports) -> list[str]:
    """The stub method(s) of one edge, shaped like the runtime navigator.

    Raises:
        NavGenerationError: A naming prop of *child* is not a field of its model.
    """
    model = _model(child)
    node = _node_return(child, flavour, imports)
    naming, types, enums = _naming_params(model)
    missing = [prop for prop in naming if prop not in types]
    if missing:
        raise NavGenerationError(f"{child}: naming props {missing} are not model fields")
    for module, enum in sorted(enums):
        imports.add(module, enum)

    if not naming:
        if child in RS_TARGET_PROP:
            return [f'    def {name}(self, target_name: str = "", **kwargs: object) -> {node}: ...']
        return [f"    def {name}(self) -> {node}: ..."]

    imports.add("typing", "overload")
    imports.add(flavour.query_module, flavour.query)
    query = f"{flavour.query}[{child}]"
    if naming == ["name"]:
        imports.add("typing", "Literal")
        return [
            "    @overload",
            f'    def {name}(self, name: Literal[""] = "") -> {query}: ...',
            "    @overload",
            f"    def {name}(self, name: str) -> {node}: ...",
        ]

    fields = model.model_fields
    required = {prop for prop in naming if fields[prop].is_required()}
    # No keyword → Query at runtime.  With a required prop, one node overload
    # suffices; with every prop optional, a single overload would swallow the
    # empty call, so each prop anchors its own overload (that prop required).
    anchors: list[frozenset[str]] = (
        [frozenset(required)] if required else [frozenset({prop}) for prop in naming]
    )
    lines = ["    @overload", f"    def {name}(self) -> {query}: ..."]
    for anchor in anchors:
        params = ", ".join(
            f"{prop}: {types[prop]}" + ("" if prop in anchor else " = ...") for prop in naming
        )
        lines += ["    @overload", f"    def {name}(self, *, {params}) -> {node}: ..."]
    return lines


def _class_lines(header: str, body: list[str]) -> list[str]:
    """A class statement: its header, then its body (``...`` when empty)."""
    return [header, *(body or ["    ..."]), ""]


# ── Modules ───────────────────────────────────────────────────────────────────


def _module_text(imports: _Imports, body: list[str], defined: Iterable[str]) -> str:
    """Banner (plus the overlap header where overloads exist), imports, body.

    Raises:
        NavGenerationError: A class the module defines (*defined*) has the
            name of something it imports.
    """
    shadowed = sorted(set(defined) & set(imports.names))
    if shadowed:
        raise NavGenerationError(
            f"{_NAV_PACKAGE}.{imports.here} defines {shadowed}, also imported there"
        )
    code = "\n".join(body)
    header = _BANNER + (_OVERLOAD_HEADER if "@overload" in code else "")
    return f"{header}{imports.render()}\n{code}\n"


def _render_base() -> str:
    """``_base.pyi``: sentinel, sealing mixin, universal mixins, leaf and end."""
    imports = _Imports(_BASE_MODULE)
    imports.add("typing", "final")
    imports.add("niwaki.models.base", "ManagedObject")
    body = [
        "@final",
        "class NotANavigationName:",
        '    """What an unknown attribute of a typed node is: nothing, not even callable."""',
        "",
        "class _Sealed:",
        '    """Turns a typo on a typed node into a static error (a call on the sentinel)."""',
        "",
        "    def __getattr__(self, name: str) -> NotANavigationName: ...",
        "",
    ]
    for flavour in _FLAVOURS:
        edges = _edges_of(_ROOT_KEY, _UNIVERSAL_NAMES)
        methods = [
            line for edge, child in edges for line in _edge_lines(edge, child, flavour, imports)
        ]
        body += _class_lines(
            f"class {flavour.universal}:",
            [f'    """The edges every generated class carries ({flavour.node})."""', *methods],
        )
    for flavour in _FLAVOURS:
        imports.add("niwaki.facade", flavour.node)
        pre = flavour.prefix
        body += [
            f"class {pre}NavEnd[T: ManagedObject](_Sealed, {flavour.node}[T]):",
            '    """A node whose class has no navigation edge at all."""',
            "",
            f"class {pre}NavLeaf[T: ManagedObject]"
            f"(_Sealed, {flavour.universal}, {flavour.node}[T]):",
            '    """A node whose class has the universal edges only."""',
            "",
        ]
    defined = [
        "NotANavigationName",
        "_Sealed",
        *(flavour.universal for flavour in _FLAVOURS),
        *(f"{flavour.prefix}{family}" for flavour in _FLAVOURS for family in ("NavEnd", "NavLeaf")),
    ]
    return _module_text(imports, body, defined)


def _bases(cls: str, flavour: _Flavour, imports: _Imports, *, sealed: bool) -> list[str]:
    """``_Sealed`` first (or ``__getattr__ -> Any`` wins by MRO), then the universal mixin."""
    bases: list[str] = []
    if sealed:
        imports.nav(_BASE_MODULE, "_Sealed")
        bases.append("_Sealed")
    if _has_universal(cls):
        imports.nav(_BASE_MODULE, flavour.universal)
        bases.append(flavour.universal)
    return bases


def _own_edges(cls: str) -> list[tuple[str, str]]:
    """The edges a typed class declares itself (the mixin covers the universal ones)."""
    names = _specific(cls) if _has_universal(cls) else _row(cls)
    return _edges_of(cls, names)


def _render_package(pkg: str, classes: list[str]) -> str:
    """``<pkg>.pyi``: the typed node classes of one ACI class package."""
    imports = _Imports(pkg)
    body: list[str] = []
    for flavour in _FLAVOURS:
        imports.add("niwaki.facade", flavour.node)
        for cls in classes:
            imports.model(cls)
            bases = [*_bases(cls, flavour, imports, sealed=True), f"{flavour.node}[{cls}]"]
            methods = [
                line
                for edge, child in _own_edges(cls)
                for line in _edge_lines(edge, child, flavour, imports)
            ]
            body += _class_lines(f"class {flavour.prefix}{cls}Node({', '.join(bases)}):", methods)
    defined = [f"{flavour.prefix}{cls}Node" for flavour in _FLAVOURS for cls in classes]
    return _module_text(imports, body, defined)


def _render_root() -> str:
    """``_root.pyi``: the client's typed base and the typed ``aci.root``."""
    imports = _Imports(_ROOT_MODULE)
    imports.add("niwaki.models.base", "ManagedObject")
    body: list[str] = []
    for flavour in _FLAVOURS:
        imports.add("niwaki.facade", flavour.node)
        imports.nav(_BASE_MODULE, "_Sealed")
        pre = flavour.prefix
        nav_bases = _bases(_ROOT_KEY, flavour, imports, sealed=False)
        methods = [
            line
            for edge, child in _own_edges(_ROOT_KEY)
            for line in _edge_lines(edge, child, flavour, imports)
        ]
        parents = f"({', '.join(nav_bases)})" if nav_bases else ""
        header = f"class {pre}RootNav{parents}:"
        body += _class_lines(
            header,
            [
                f'    """The top-level navigation of the {pre}Niwaki client (not sealed: the',
                '    client has its own API, and an unknown name stays an attribute error)."""',
                *methods,
            ],
        )
        body += _class_lines(
            f"class {pre}RootNode(_Sealed, {pre}RootNav, {flavour.node}[ManagedObject]):",
            ['    """``aci.root``: the typed node at DN ``uni``."""'],
        )
    defined = [
        f"{flavour.prefix}{name}" for flavour in _FLAVOURS for name in ("RootNav", "RootNode")
    ]
    return _module_text(imports, body, defined)


def _render_init() -> str:
    """``__init__.pyi``: explicit re-exports of the base and root names."""
    exports = {
        _BASE_MODULE: [
            "NotANavigationName",
            *(
                f"{flavour.prefix}{family}"
                for flavour in _FLAVOURS
                for family in ("NavEnd", "NavLeaf")
            ),
        ],
        _ROOT_MODULE: [
            f"{flavour.prefix}{name}" for flavour in _FLAVOURS for name in ("RootNav", "RootNode")
        ],
    }
    lines = [
        f"from {_NAV_PACKAGE}.{module} import {name} as {name}"
        for module, names in exports.items()
        for name in sorted(names)
    ]
    return _BANNER + "\n".join(lines) + "\n"


def _ruff(texts: Mapping[Path, str]) -> dict[Path, str]:
    """Run ``ruff format`` and ``ruff check --fix`` (repo config) over *texts*.

    Raises:
        NavGenerationError: ruff reports a violation it cannot fix, or fails.
    """
    ruff = [sys.executable, "-m", "ruff"]
    config = ["--config", str(_PYPROJECT)]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "_generated_nav"
        root.mkdir()
        for rel, text in texts.items():
            (root / rel).write_text(text, encoding="utf-8")
        # Format first (long signatures wrap), fix (import order), format again
        # (the fix may reflow the import block).
        fmt = [*ruff, "format", "--quiet", *config, str(root)]
        for command in (fmt, [*ruff, "check", "--fix", "--quiet", *config, str(root)], fmt):
            run = subprocess.run(command, capture_output=True, text=True, check=False)
            if run.returncode != 0:
                raise NavGenerationError(
                    f"{' '.join(command[2:4])} failed on the navigation stubs:\n"
                    f"{run.stdout}{run.stderr}"
                )
        return {rel: (root / rel).read_text(encoding="utf-8") for rel in texts}


# ── Public API ────────────────────────────────────────────────────────────────


def render_nav() -> dict[Path, str]:
    """Render every navigation stub file, without writing anything.

    The texts are computed from the committed tables and models (no schema
    corpus), then passed through ``ruff check --fix`` and ``ruff format`` with
    the repository configuration — in a scratch directory, never in the
    package — so each value is the exact committed file content.

    Returns:
        ``{relative path: file text}``, keys sorted.  Keys are paths relative
        to the stub package directory: ``__init__.pyi``, ``_base.pyi``,
        ``_root.pyi`` and one ``<pkg>.pyi`` per ACI class package holding a
        typed parent — never a ``.py``.  Every text starts with the
        "Generated by … do not edit manually" banner; every text that uses
        ``@overload`` also carries ``# mypy: disable-error-code=
        "overload-overlap"`` in that leading comment block.  Each ends with a
        newline.  Two calls return equal dicts in the same key order.

    Raises:
        NavGenerationError: The tables have a shape the stubs cannot describe
            faithfully (see :class:`NavGenerationError`), or ruff refuses the
            rendered text.

    Example:
        >>> stubs = render_nav()  # doctest: +SKIP
        >>> sorted(stubs)[:3]  # doctest: +SKIP
        [PosixPath('__init__.pyi'), PosixPath('_base.pyi'), PosixPath('_root.pyi')]
    """
    # Validate the universal edges before anything indexes the _root row for
    # them, so a missing one is refused with its name, never a bare KeyError.
    _universal_row()
    by_pkg: dict[str, list[str]] = {}
    for cls in sorted(_typed_parents()):
        by_pkg.setdefault(CLASS_PKG[cls], []).append(cls)
    raw: dict[Path, str] = {
        Path("__init__.pyi"): _render_init(),
        Path(f"{_BASE_MODULE}.pyi"): _render_base(),
        Path(f"{_ROOT_MODULE}.pyi"): _render_root(),
    }
    for pkg, classes in by_pkg.items():
        if pkg in {_BASE_MODULE, _ROOT_MODULE, "__init__"}:
            raise NavGenerationError(f"ACI package {pkg!r} collides with a stub module name")
        raw[Path(f"{pkg}.pyi")] = _render_package(pkg, classes)
    formatted = _ruff(raw)
    return {rel: formatted[rel] for rel in sorted(formatted)}


def write_nav(nav_dir: Path | None = None) -> None:
    """Write the navigation stubs and delete the ones the render no longer produces.

    Every file of :func:`render_nav` is written under *nav_dir* — created
    when missing — and rewritten only when its content changed, so an
    unchanged stub keeps its modification time.  Then every ``*.pyi`` in
    *nav_dir* that the render did not produce is deleted.  Nothing else in
    the directory (a ``__pycache__``, say) is touched, and no ``.py`` is ever
    written: a single one would make the stub package importable.

    Args:
        nav_dir: The stub package directory.  ``None`` means the package's
            own ``src/niwaki/_generated_nav``.

    Raises:
        NavGenerationError: Propagated from :func:`render_nav`.

    Example:
        >>> write_nav()  # doctest: +SKIP
    """
    target = _NAV_DIR if nav_dir is None else nav_dir
    rendered = render_nav()
    target.mkdir(parents=True, exist_ok=True)
    for rel, text in rendered.items():
        path = target / rel
        if path.is_file() and path.read_text(encoding="utf-8") == text:
            continue
        path.write_text(text, encoding="utf-8")
    for stale in sorted(target.glob("*.pyi")):
        if stale.relative_to(target) not in rendered:
            stale.unlink()


def main() -> None:
    """Entry point of ``python -m niwaki._codegen.generate_nav``."""
    write_nav()
    print(f"navigation stubs written under {_NAV_DIR}")


if __name__ == "__main__":
    main()
