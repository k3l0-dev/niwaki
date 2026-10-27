"""The shipped read catalogue has exactly one door: :class:`niwaki.query._catalog.Catalog`.

Three runtime consumers used to open ``catalog.db`` themselves with
``sqlite3.connect`` and raw SQL — ``snapshot._snapshot_universe``,
``design._reconcile._never_creatable`` and ``_secrets._secure_flagged`` (the
last one straight against the ``prop`` table).  Any change to the db's storage
layout (normalising ``prop`` into shared signatures, compressing the file)
would then break them behind the reader's back — and ``_secrets`` breaking
means secrets stop being redacted.  This module pins the single access point
from four sides:

1. **Structure** — an AST scan of ``src/niwaki`` (the build-time
   ``_codegen`` package excluded: it *writes* the db) proving that
   ``sqlite3.connect`` is reachable from one module only, the reader itself.
2. **Golden answers** — the three consumers' results on the shipped db,
   frozen as length + SHA-256 *before* the refactor.  They hold before and
   after: the refactor must not move a single class or property.  No raw
   schema corpus is needed, so this runs on the public CI too.
3. **The reader's private contract** — the methods the three consumers call
   instead of their own SQL, exercised on a tiny catalogue built from
   synthetic schemas by the real generator.
4. **A missing db** — every consumer raises the reader's own explicit
   :class:`FileNotFoundError` (the "broken or partial install" message)
   instead of a raw ``sqlite3.OperationalError``.  An intended behaviour
   change, pinned here.

**The reader contract** (what the three consumers call)::

    Catalog._secure_props()            -> frozenset[tuple[str, str]]
        every (wire class, wire prop) whose schema flags carry ``secure``
    Catalog._exportable_classes()      -> frozenset[str]
        concrete (non-abstract) classes the schema marks ``isExportable``,
        minus those whose ``moCategory`` is ``FaultCurrent`` (runtime
        state, not configuration — the snapshot's "keep" set)
    Catalog._concrete_classes()        -> frozenset[str]
        every non-abstract class (the snapshot's ancestor-closure filter)
    Catalog._never_creatable_classes() -> frozenset[str]
        every class whose ``isCreatableDeletable`` is not ``"always"``

They are private (underscore) and not part of the public ``niwaki.catalog``
surface.  The consumers reach them through the process-wide reader
:func:`niwaki.query._catalog.catalog` — that singleton is the hook the
missing-db tests patch.

**Caches.**  ``_secure_flagged`` is memoised with :func:`functools.cache`
(and a consumer may grow one in the refactor).  A memoised answer never
touches the db, so a missing-db test could pass or fail on test order rather
than on behaviour, and a golden test could read an answer computed before the
monkeypatch.  :func:`_clear_consumer_caches` clears every ``cache_clear`` the
three consumers expose; it runs before and after each test here
(``autouse`` fixture), so the tests are order-independent and leave no
patched state behind.  The ``Catalog`` instances' own memo dicts need no
clearing: each test that patches the db installs a *fresh* instance.
"""

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest

from niwaki import _secrets, snapshot
from niwaki._codegen import generate_catalog as gc
from niwaki.design import _reconcile
from niwaki.query import _catalog

SRC_ROOT = Path(_catalog.__file__).resolve().parents[2]  # .../src/niwaki
ALLOWED_OPENER = Path("query/_catalog/__init__.py")
EXCLUDED_PACKAGES = frozenset({"_codegen"})


# ── Cache hygiene ─────────────────────────────────────────────────────────────


_CONSUMERS: tuple[Callable[[], object], ...] = (
    _secrets._secure_flagged,
    snapshot._snapshot_universe,
    _reconcile._never_creatable,
)


def _clear_consumer_caches() -> None:
    """Drop every memoised consumer answer (``functools.cache``-style).

    Duck-typed on ``cache_clear`` so a cache the refactor adds is cleared too,
    without this module having to know which consumer grew one.
    """
    for consumer in _CONSUMERS:
        clear: Callable[[], None] | None = getattr(consumer, "cache_clear", None)
        if clear is not None:
            clear()


@pytest.fixture(autouse=True)
def _fresh_consumer_caches() -> Iterator[None]:
    _clear_consumer_caches()
    yield
    _clear_consumer_caches()


def _call_private(cat: _catalog.Catalog, name: str) -> object:
    """Call a reader method that may not exist yet (RED until it does).

    Reached through ``getattr`` so this module type-checks before the
    implementation lands; a missing method fails the test with a clear
    message instead of an ``AttributeError`` deep in a fixture.
    """
    method: Callable[[], object] | None = getattr(cat, name, None)
    if method is None:
        pytest.fail(f"Catalog.{name}() does not exist yet — the B1 reader contract")
    return method()


# ── 1. Structure: one module opens the db ─────────────────────────────────────


def _connect_sites(source: str) -> list[int]:
    """Line numbers where ``source`` reaches ``sqlite3.connect``.

    Catches the module under any alias (``import sqlite3 as s`` →
    ``s.connect``), a bare reference (``opener = sqlite3.connect``), and the
    function imported directly (``from sqlite3 import connect [as c]``).
    """
    tree = ast.parse(source)
    module_aliases: set[str] = set()
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sqlite3":
                    module_aliases.add(alias.asname or "sqlite3")
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module == "sqlite3"
            and any(alias.name in {"connect", "*"} for alias in node.names)
        ):
            lines.append(node.lineno)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "connect"
            and isinstance(node.value, ast.Name)
            and node.value.id in module_aliases
        ):
            lines.append(node.lineno)
    return sorted(lines)


def _scanned_modules() -> Iterator[Path]:
    """Every ``.py`` under ``src/niwaki`` outside the excluded build packages.

    Pre-filtered on the text ``sqlite3``: a module that never spells the
    name cannot import it, and skipping the ~3 000 generated model files
    keeps the scan instant.
    """
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(SRC_ROOT)
        if rel.parts[0] in EXCLUDED_PACKAGES:
            continue
        if "sqlite3" in path.read_text(encoding="utf-8"):
            yield path


class TestScanner:
    """The AST scanner itself — so the guard below cannot pass vacuously."""

    def test_plain_module_call_is_found(self) -> None:
        assert _connect_sites("import sqlite3\ncon = sqlite3.connect('x')\n") == [2]

    def test_function_local_import_is_found(self) -> None:
        source = "def f():\n    import sqlite3\n    return sqlite3.connect('x')\n"
        assert _connect_sites(source) == [3]

    def test_aliased_module_is_found(self) -> None:
        assert _connect_sites("import sqlite3 as db\ncon = db.connect('x')\n") == [2]

    def test_bare_reference_is_found(self) -> None:
        assert _connect_sites("import sqlite3\nopener = sqlite3.connect\n") == [2]

    def test_direct_function_import_is_found(self) -> None:
        assert _connect_sites("from sqlite3 import connect as c\n") == [1]
        assert _connect_sites("from sqlite3 import *\n") == [1]

    def test_unrelated_uses_are_not_flagged(self) -> None:
        source = (
            "import sqlite3\n"
            "from sqlite3 import Row\n"
            "err = sqlite3.OperationalError\n"
            "import other\n"
            "other.connect('x')\n"
            "connect = 1\n"
        )
        assert _connect_sites(source) == []

    def test_the_allowed_module_really_opens_the_db(self) -> None:
        """The allow-listed path is the live reader, not a stale name.

        If the reader moved, the guard below would silently allow nothing in
        particular; this pins that the exemption still points at the one
        module that does connect.
        """
        source = (SRC_ROOT / ALLOWED_OPENER).read_text(encoding="utf-8")
        assert _connect_sites(source), f"{ALLOWED_OPENER} no longer calls sqlite3.connect"


def test_only_the_catalogue_reader_opens_sqlite() -> None:
    """``sqlite3.connect`` lives in ``niwaki/query/_catalog/__init__.py`` only.

    Every other runtime module reads the catalogue through ``Catalog`` — the
    one place that knows the storage layout, the ``immutable``/
    ``cached_statements=0`` connection discipline, and the explicit
    missing-file error.
    """
    offenders = [
        f"{path.relative_to(SRC_ROOT)}:{line}"
        for path in _scanned_modules()
        if path.relative_to(SRC_ROOT) != ALLOWED_OPENER
        for line in _connect_sites(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], (
        "sqlite3.connect outside the catalogue reader — route these through "
        f"niwaki.query._catalog.Catalog: {offenders}"
    )


# ── 2. Golden answers on the shipped db (frozen before the refactor) ──────────


def _digest(items: Iterable[str]) -> str:
    """SHA-256 of the sorted items, newline-joined — order-independent."""
    return hashlib.sha256("\n".join(sorted(items)).encode()).hexdigest()


def _pairs(pairs: Iterable[tuple[str, str]]) -> list[str]:
    """``(class, prop)`` pairs as ``class\\tprop`` lines (tab: in neither name)."""
    return [f"{cls}\t{prop}" for cls, prop in pairs]


# Frozen on the shipped catalogue (APIC 6.0(9c)) on 2026-10-26, before B1.
SECURE_FLAGGED_LEN = 169
SECURE_FLAGGED_SHA256 = "5a9e6f299af6efb83cea9444dbede73e1f6d390ec44365451bc388169a23b2d1"
# _snapshot_universe() -> (request, keep).  ``keep`` is pure catalogue;
# ``request`` adds the CHILD_MAP ancestor closure (filtered to concrete
# classes) — a regen that changes CHILD_MAP legitimately re-freezes it.
SNAPSHOT_REQUEST_LEN = 2512
SNAPSHOT_REQUEST_SHA256 = "5c3e0128ed5ac1ff41d01f345dc549af8c7af3e77935519717ed1a10952d8689"
SNAPSHOT_KEEP_LEN = 2112
SNAPSHOT_KEEP_SHA256 = "b5cf8381b55e7c0a855624a84c8e85758d0a9c0b9294185627ecea22090a9beb"
NEVER_CREATABLE_LEN = 10219
NEVER_CREATABLE_SHA256 = "cdb3dc4bb6bbf394fb6fe00e99d2ab864a0c8f7fb4e38438e8685f2d381003cc"


class TestGoldenAnswers:
    """The three consumers answer exactly as they did before the refactor."""

    def test_secure_flagged(self) -> None:
        got = _secrets._secure_flagged()
        assert isinstance(got, frozenset)
        assert len(got) == SECURE_FLAGGED_LEN
        assert _digest(_pairs(got)) == SECURE_FLAGGED_SHA256

    def test_secure_flagged_spot_checks(self) -> None:
        """Readable anchors beside the hash — a failure names a culprit."""
        got = _secrets._secure_flagged()
        # vnsCCred.value is the curated miss (NOT flagged) — must stay out.
        assert ("vnsCCred", "value") not in got
        assert all(isinstance(c, str) and isinstance(p, str) for c, p in got)

    def test_snapshot_universe(self) -> None:
        request, keep = snapshot._snapshot_universe()
        assert isinstance(request, frozenset)
        assert isinstance(keep, frozenset)
        assert (len(request), _digest(request)) == (
            SNAPSHOT_REQUEST_LEN,
            SNAPSHOT_REQUEST_SHA256,
        )
        assert (len(keep), _digest(keep)) == (SNAPSHOT_KEEP_LEN, SNAPSHOT_KEEP_SHA256)

    def test_snapshot_universe_spot_checks(self) -> None:
        request, keep = snapshot._snapshot_universe()
        assert keep <= request
        assert "fvBD" in keep
        assert "faultInst" not in keep  # FaultCurrent: runtime state, not config

    def test_never_creatable(self) -> None:
        got = _reconcile._never_creatable()
        assert isinstance(got, frozenset)
        assert (len(got), _digest(got)) == (NEVER_CREATABLE_LEN, NEVER_CREATABLE_SHA256)

    def test_never_creatable_spot_checks(self) -> None:
        got = _reconcile._never_creatable()
        assert "fvBD" not in got  # creatable: an operator's object
        assert "fvRsCtx" in got  # a relation the fabric materialises


class TestReaderAnswersMatchTheGoldens:
    """The new reader methods, on the shipped db, carry the same goldens.

    RED until the methods exist.  The pin a later storage change (``prop``
    normalisation) must hold at the reader's own level.
    """

    def test_secure_props(self) -> None:
        got = _call_private(_catalog.catalog(), "_secure_props")
        assert isinstance(got, frozenset)
        assert len(got) == SECURE_FLAGGED_LEN
        assert _digest(_pairs(got)) == SECURE_FLAGGED_SHA256

    def test_exportable_classes(self) -> None:
        got = _call_private(_catalog.catalog(), "_exportable_classes")
        assert isinstance(got, frozenset)
        assert (len(got), _digest(got)) == (SNAPSHOT_KEEP_LEN, SNAPSHOT_KEEP_SHA256)

    def test_never_creatable_classes(self) -> None:
        got = _call_private(_catalog.catalog(), "_never_creatable_classes")
        assert isinstance(got, frozenset)
        assert (len(got), _digest(got)) == (NEVER_CREATABLE_LEN, NEVER_CREATABLE_SHA256)

    def test_concrete_classes_bound_the_request_set(self) -> None:
        concrete = _call_private(_catalog.catalog(), "_concrete_classes")
        assert isinstance(concrete, frozenset)
        request, keep = snapshot._snapshot_universe()
        assert keep <= concrete
        assert request <= concrete
        assert "fvBD" in concrete


# ── 3. The reader's private contract, on a tiny synthetic catalogue ───────────


def _prop(*, secure: bool = False, naming: bool = False) -> dict[str, Any]:
    """A property carrying all 14 schema flags, as the corpus always does."""
    flags: dict[str, Any] = {flag: False for flag in gc.PROP_FLAG_ORDER}
    flags.update(baseType="string:Basic", isConfigurable=True, secure=secure, isNaming=naming)
    return flags


def _cls(
    name: str,
    *,
    abstract: bool,
    exportable: bool,
    category: str,
    creatable: str,
    props: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """A schema class with every column the contract reads set explicitly.

    No column is left absent (NULL in the db): the contract is pinned on
    real schema values only, not on how SQL three-valued logic treats NULL.
    """
    return {
        "classPkg": "xy",
        "className": name,
        "rnFormat": f"{name.lower()}-{{name}}",
        "isAbstract": abstract,
        "isExportable": exportable,
        "isConfigurable": True,
        "moCategory": category,
        "isCreatableDeletable": creatable,
        "properties": props,
    }


#: The synthetic universe.  Each class isolates one axis of the contract.
_SYNTHETIC: dict[str, dict[str, Any]] = {
    # Concrete, exportable, configuration, creatable — the nominal object.
    "Cfg": _cls(
        "Cfg",
        abstract=False,
        exportable=True,
        category="Regular",
        creatable="always",
        props={"name": _prop(naming=True), "key": _prop(secure=True)},
    ),
    # A relation the fabric materialises: exportable but never creatable.
    "RsLink": _cls(
        "RsLink",
        abstract=False,
        exportable=True,
        category="RelationshipToLocal",
        creatable="never",
        props={"tDn": _prop()},
    ),
    # Exportable and concrete, but runtime state (FaultCurrent) → not kept.
    "Fault": _cls(
        "Fault",
        abstract=False,
        exportable=True,
        category="FaultCurrent",
        creatable="never",
        props={"code": _prop()},
    ),
    # Abstract: never kept, never concrete; "derived" is not "always".
    "Abstract": _cls(
        "Abstract",
        abstract=True,
        exportable=True,
        category="Regular",
        creatable="derived",
        props={"name": _prop(naming=True)},
    ),
    # Concrete but not exportable (runtime) — a secret here still counts:
    # the secure set is schema-wide, not snapshot-scoped.
    "Session": _cls(
        "Session",
        abstract=False,
        exportable=False,
        category="Regular",
        creatable="always",
        props={"token": _prop(secure=True), "user": _prop()},
    ),
}


@pytest.fixture(scope="module")
def tiny_catalog(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_catalog.Catalog]:
    """A catalogue built by the real generator from :data:`_SYNTHETIC`.

    ``build_catalog`` refuses a db that lacks a row for any *generated* model
    (its fail-loud completeness check against ``_PKG_MAP``); the synthetic
    universe has none of them, so the index is emptied for the build only.
    """
    root = tmp_path_factory.mktemp("tiny_catalog")
    schemas = root / "schemas"
    schemas.mkdir()
    for name, cls in _SYNTHETIC.items():
        (schemas / f"xy{name}.json").write_text(json.dumps({f"xy:{name}": cls}))
    out = root / "catalog.db"
    with pytest.MonkeyPatch.context() as mp:
        from niwaki.models import _generated

        mp.setattr(_generated, "_PKG_MAP", {})
        gc.build_catalog(schemas, out)
    cat = _catalog.Catalog(out)
    yield cat
    cat.close()


class TestSecureProps:
    def test_nominal_every_secure_flag_schema_wide(self, tiny_catalog: _catalog.Catalog) -> None:
        got = _call_private(tiny_catalog, "_secure_props")
        assert got == frozenset({("xyCfg", "key"), ("xySession", "token")})
        assert isinstance(got, frozenset)

    def test_a_class_without_secrets_is_absent(self, tiny_catalog: _catalog.Catalog) -> None:
        got = _call_private(tiny_catalog, "_secure_props")
        assert isinstance(got, frozenset)
        classes = {pair[0] for pair in got}
        assert "xyRsLink" not in classes  # present in the db, no secure prop
        assert "xyNoSuchClass" not in classes  # absent from the db entirely
        assert ("xyCfg", "name") not in got  # non-secure sibling of a secret

    def test_answer_is_stable_across_calls(self, tiny_catalog: _catalog.Catalog) -> None:
        assert _call_private(tiny_catalog, "_secure_props") == _call_private(
            tiny_catalog, "_secure_props"
        )


class TestExportableClasses:
    def test_nominal_concrete_exportable_configuration(
        self, tiny_catalog: _catalog.Catalog
    ) -> None:
        got = _call_private(tiny_catalog, "_exportable_classes")
        assert got == frozenset({"xyCfg", "xyRsLink"})
        assert isinstance(got, frozenset)

    def test_excluded_and_absent_classes(self, tiny_catalog: _catalog.Catalog) -> None:
        got = _call_private(tiny_catalog, "_exportable_classes")
        assert isinstance(got, frozenset)
        assert "xyFault" not in got  # FaultCurrent: runtime state
        assert "xyAbstract" not in got  # abstract
        assert "xySession" not in got  # not exportable
        assert "xyNoSuchClass" not in got  # absent from the db


class TestConcreteClasses:
    def test_nominal_every_non_abstract_class(self, tiny_catalog: _catalog.Catalog) -> None:
        got = _call_private(tiny_catalog, "_concrete_classes")
        assert got == frozenset({"xyCfg", "xyRsLink", "xyFault", "xySession"})
        assert isinstance(got, frozenset)

    def test_abstract_and_absent_classes(self, tiny_catalog: _catalog.Catalog) -> None:
        got = _call_private(tiny_catalog, "_concrete_classes")
        assert isinstance(got, frozenset)
        assert "xyAbstract" not in got
        assert "xyNoSuchClass" not in got


class TestNeverCreatableClasses:
    def test_nominal_everything_not_always(self, tiny_catalog: _catalog.Catalog) -> None:
        got = _call_private(tiny_catalog, "_never_creatable_classes")
        # "never" and "derived" both count: anything but "always".
        assert got == frozenset({"xyRsLink", "xyFault", "xyAbstract"})
        assert isinstance(got, frozenset)

    def test_creatable_and_absent_classes(self, tiny_catalog: _catalog.Catalog) -> None:
        got = _call_private(tiny_catalog, "_never_creatable_classes")
        assert isinstance(got, frozenset)
        assert "xyCfg" not in got
        assert "xySession" not in got
        assert "xyNoSuchClass" not in got


@pytest.mark.parametrize(
    "method",
    ["_secure_props", "_exportable_classes", "_concrete_classes", "_never_creatable_classes"],
)
def test_reader_methods_raise_the_explicit_missing_db_error(tmp_path: Path, method: str) -> None:
    cat = _catalog.Catalog(tmp_path / "absent.db")
    with pytest.raises(FileNotFoundError, match="read catalogue not found"):
        _call_private(cat, method)


# ── 4. A missing db: the consumers raise the reader's explicit error ──────────


@pytest.fixture
def missing_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the process-wide reader (and the default path) at a missing file.

    The consumers reach the catalogue through :func:`_catalog.catalog`, whose
    singleton is replaced by a *fresh* reader on the missing path — fresh, so
    no memoised answer from an earlier test can hide the missing file.
    ``DEFAULT_PATH`` is patched as well, for a consumer that builds
    ``Catalog(_catalog.DEFAULT_PATH)`` at call time.  Both revert at teardown.
    """
    absent = tmp_path / "absent" / "catalog.db"
    monkeypatch.setattr(_catalog, "DEFAULT_PATH", absent)
    monkeypatch.setattr(_catalog, "_instance", _catalog.Catalog(absent))
    return absent


class TestMissingDb:
    """Before B1 these raised ``sqlite3.OperationalError`` ("unable to open
    database file") — for ``_secrets`` that meant no redaction policy at all,
    behind an error that names neither the file nor the fix."""

    def test_secure_flagged(self, missing_db: Path) -> None:
        with pytest.raises(FileNotFoundError, match="read catalogue not found"):
            _secrets._secure_flagged()

    def test_is_secret_prop_goes_through_the_same_door(self, missing_db: Path) -> None:
        # A non-curated position forces the schema-flag lookup.
        with pytest.raises(FileNotFoundError, match="read catalogue not found"):
            _secrets.is_secret_prop("xyNoSuchClass", "noSuchProp")

    def test_snapshot_universe(self, missing_db: Path) -> None:
        with pytest.raises(FileNotFoundError, match="read catalogue not found"):
            snapshot._snapshot_universe()

    def test_never_creatable(self, missing_db: Path) -> None:
        with pytest.raises(FileNotFoundError, match="read catalogue not found"):
            _reconcile._never_creatable()

    def test_the_error_names_the_missing_path(self, missing_db: Path) -> None:
        with pytest.raises(FileNotFoundError) as info:
            _reconcile._never_creatable()
        assert str(missing_db) in str(info.value)
