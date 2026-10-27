"""The public model import path is a real, statically-resolvable mirror.

Every generated module lives under ``niwaki.models._generated``; the documented
import path drops that segment::

    from niwaki.models.fv.fvBD import fvBD           # public path
    from niwaki.models._generated.fv.fvBD import fvBD  # canonical path

The public path is a **generated mirror of real re-export modules** — one file
per generated module, e.g. ``src/niwaki/models/fv/fvBD.py`` containing
``from niwaki.models._generated.fv.fvBD import fvBD as fvBD`` (the PEP 484
explicit re-export form, which mypy and pyright both resolve).  Package
``__init__`` files of the mirror resolve the short names (``fv.BD``) lazily via
a module ``__getattr__``, exactly like their canonical counterparts.  No
``MetaPathFinder`` is involved: an import hook is invisible to static analysis,
which is the defect this mirror removes.

This module pins:

1. **Exhaustive runtime parity** — for every module under ``_generated``,
   enumerated from the filesystem (never from a hand list), the public path
   imports and exposes the *same objects* (``is``) with the same ``__all__``.
2. **Real files** — each public module is backed by its own file in the
   mirror, not served from the canonical file.
3. **No import hook** — no niwaki finder sits in ``sys.meta_path``.
4. **Module identity** — the alias *module* is a distinct object from the
   canonical one, while the *class* is the very same object whose
   ``__module__`` (hence pickles, ``repr``, the REGISTRY) stays canonical.
5. **Laziness** — importing a mirror package loads none of its classes; a
   short-name access loads only the class it names.
6. **Error paths** — unknown short names raise ``AttributeError`` naming the
   public module; unknown submodules raise ``ModuleNotFoundError``.
7. **Static resolution** — the ``_static_*`` functions at the bottom are never
   called; they exist for pyright (which type-checks ``tests/`` in
   ``scripts/checks.sh --fast``).  An unresolved import is ``Unknown`` to
   pyright and every ``assert_type`` below then fails statically.
"""

from __future__ import annotations

import importlib
import pickle
import subprocess
import sys
from importlib.abc import MetaPathFinder
from pathlib import Path
from types import ModuleType
from typing import Literal, assert_type

import pytest

import niwaki.models
from niwaki.models._generated.enums._all import FvBDType as _CanonicalFvBDTypeFromAll
from niwaki.models._generated.enums.FvBDType import FvBDType as _CanonicalFvBDType
from niwaki.models._generated.fv.fvBD import fvBD as _CanonicalFvBD
from niwaki.models.base import REGISTRY, ManagedObject

# The public imports under test.  At runtime they double as a smoke check; for
# pyright they are the subject of the ``_static_*`` assertions at the bottom.
from niwaki.models.enums._all import FvBDType as FvBDTypeFromAll
from niwaki.models.enums.FvBDType import FvBDType
from niwaki.models.fv.fvBD import fvBD

_MODELS = Path(niwaki.models.__file__).resolve().parent
_GENERATED = _MODELS / "_generated"
_ROOT = _MODELS.parents[2]  # repo root (src/niwaki/models → repo)

_CANONICAL_PREFIX = "niwaki.models._generated"
_PUBLIC_PREFIX = "niwaki.models"


# ── Enumeration — the filesystem is the only source of truth ──────────────────


def _generated_modules() -> list[Path]:
    """Every generated module except the root ``_generated/__init__.py``.

    The root has no mirror counterpart: the public root is the hand-written
    ``niwaki/models/__init__.py``.  Everything else — the 174 ACI package
    ``__init__`` files, the 2,222 model modules, ``enums/__init__.py``, the
    676 enum modules and the ``enums/_all.py`` barrel — must be mirrored.
    """
    return sorted(
        path.relative_to(_GENERATED)
        for path in _GENERATED.rglob("*.py")
        if "__pycache__" not in path.parts and path.relative_to(_GENERATED) != Path("__init__.py")
    )


def _dotted(rel: Path) -> str:
    parts = rel.with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _canonical_name(rel: Path) -> str:
    return f"{_CANONICAL_PREFIX}.{_dotted(rel)}"


def _public_name(rel: Path) -> str:
    return f"{_PUBLIC_PREFIX}.{_dotted(rel)}"


_MODULES = _generated_modules()
_IDS = [_dotted(rel) for rel in _MODULES]


def test_the_enumeration_sees_the_whole_generated_tree() -> None:
    """Guard against a vacuous parametrisation (empty glob, wrong root)."""
    kinds = {
        "packages": [r for r in _MODULES if r.name == "__init__.py"],
        "enums": [r for r in _MODULES if r.parts[0] == "enums" and r.name != "__init__.py"],
    }
    assert len(_MODULES) > 3_000, f"only {len(_MODULES)} generated modules found under {_GENERATED}"
    assert Path("fv") / "fvBD.py" in _MODULES
    assert Path("enums") / "FvBDType.py" in _MODULES
    assert Path("enums") / "_all.py" in _MODULES
    assert Path("enums") / "__init__.py" in _MODULES
    assert Path("__init__.py") not in _MODULES
    assert len(kinds["packages"]) > 100
    assert len(kinds["enums"]) > 600


# ── 1. Exhaustive runtime parity ──────────────────────────────────────────────


def _assert_same_all(public: ModuleType, canonical: ModuleType) -> None:
    sentinel = object()
    assert getattr(public, "__all__", sentinel) == getattr(canonical, "__all__", sentinel), (
        f"{public.__name__}.__all__ differs from {canonical.__name__}.__all__"
    )


@pytest.mark.parametrize("rel", _MODULES, ids=_IDS)
def test_public_path_exposes_the_same_objects(rel: Path) -> None:
    """The public module re-exports the canonical objects themselves (``is``)."""
    canonical = importlib.import_module(_canonical_name(rel))
    public = importlib.import_module(_public_name(rel))
    _assert_same_all(public, canonical)

    if rel.name == "__init__.py":
        assert hasattr(public, "__path__"), f"{public.__name__} is not a package"
        # Short names (``fv.BD``) resolve to the very same class objects.
        for short in getattr(canonical, "__all__", ()):
            assert getattr(public, short) is getattr(canonical, short), (
                f"{public.__name__}.{short} is not {canonical.__name__}.{short}"
            )
    elif rel == Path("enums") / "_all.py":
        exported = {
            name: value
            for name, value in vars(canonical).items()
            if not name.startswith("_") and isinstance(value, type)
        }
        assert exported, "the canonical enums barrel exports nothing"
        for name, value in exported.items():
            assert getattr(public, name) is value, f"{public.__name__}.{name} is not {value!r}"
    else:
        stem = rel.stem
        assert getattr(public, stem) is getattr(canonical, stem), (
            f"{public.__name__}.{stem} is not {canonical.__name__}.{stem}"
        )


# ── 2. Real files, not a redirected canonical module ─────────────────────────


def test_every_generated_module_has_a_mirror_file() -> None:
    """Static resolution needs a file on disk at the public path — for each module."""
    missing = [rel for rel in _MODULES if not (_MODELS / rel).is_file()]
    assert not missing, (
        f"{len(missing)} of {len(_MODULES)} generated modules have no mirror file "
        f"under {_MODELS} (first: {[str(m) for m in missing[:5]]})"
    )


def test_the_mirror_holds_nothing_but_mirrored_modules() -> None:
    """No stale mirror file survives a vanished generated module."""
    expected = set(_MODULES)
    stray = sorted(
        path.relative_to(_MODELS)
        for child in _MODELS.iterdir()
        if child.is_dir() and child.name not in {"_generated", "__pycache__"}
        for path in child.rglob("*.py")
        if "__pycache__" not in path.parts and path.relative_to(_MODELS) not in expected
    )
    assert not stray, f"mirror files without a generated counterpart: {[str(s) for s in stray]}"


@pytest.mark.parametrize("rel", _MODULES, ids=_IDS)
def test_public_path_is_a_real_module_file(rel: Path) -> None:
    """The public module is loaded from its own mirror file, under its own name."""
    public = importlib.import_module(_public_name(rel))
    assert public.__name__ == _public_name(rel)
    assert public.__spec__ is not None and public.__spec__.origin is not None
    assert Path(public.__spec__.origin).resolve() == (_MODELS / rel).resolve(), (
        f"{public.__name__} was loaded from {public.__spec__.origin}, "
        f"not from its mirror file {_MODELS / rel}"
    )


# ── 3. No import hook ─────────────────────────────────────────────────────────

_NIWAKI_FINDERS_PROBE = (
    "import sys\n"
    "{imports}\n"
    "found = [\n"
    "    f'{{type(f).__module__}}.{{type(f).__qualname__}}'\n"
    "    for f in sys.meta_path\n"
    "    if type(f).__module__ == 'niwaki' or type(f).__module__.startswith('niwaki.')\n"
    "]\n"
    "print(','.join(found))\n"
)


@pytest.mark.parametrize(
    "imports",
    [
        "import niwaki",
        "import niwaki.models",
        "from niwaki import Niwaki",
        # An alias import is exactly what a lazily-installed hook would serve.
        "import niwaki\nfrom niwaki.models.fv.fvBD import fvBD\nimport niwaki.models.enums._all",
    ],
    ids=["import-niwaki", "import-models", "from-niwaki", "after-alias-import"],
)
def test_no_niwaki_finder_in_meta_path(imports: str) -> None:
    """The mirror is plain files: niwaki installs no ``MetaPathFinder``.

    Checked in a fresh interpreter so no earlier test can mask the result.
    """
    completed = subprocess.run(
        [sys.executable, "-c", _NIWAKI_FINDERS_PROBE.format(imports=imports)],
        capture_output=True,
        cwd=_ROOT,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    found = completed.stdout.strip()
    assert not found, f"niwaki import hook(s) still installed in sys.meta_path: {found}"


def test_models_package_defines_no_finder_class() -> None:
    """The hook class itself is gone, not merely left uninstalled."""
    finders = [
        name
        for name, value in vars(niwaki.models).items()
        if isinstance(value, type)
        and issubclass(value, MetaPathFinder)
        and value.__module__.startswith("niwaki")
    ]
    assert not finders, f"niwaki.models still defines import-hook classes: {finders}"


# ── 4. Module identity vs class identity ─────────────────────────────────────


def test_alias_model_module_is_distinct_from_canonical_module() -> None:
    canonical = importlib.import_module("niwaki.models._generated.fv.fvBD")
    public = importlib.import_module("niwaki.models.fv.fvBD")

    assert public is not canonical
    assert public.__name__ == "niwaki.models.fv.fvBD"
    assert canonical.__name__ == "niwaki.models._generated.fv.fvBD"
    assert sys.modules["niwaki.models.fv.fvBD"] is public
    assert sys.modules["niwaki.models._generated.fv.fvBD"] is canonical


def test_alias_package_is_distinct_from_canonical_package() -> None:
    canonical = importlib.import_module("niwaki.models._generated.fv")
    public = importlib.import_module("niwaki.models.fv")

    assert public is not canonical
    assert public.__name__ == "niwaki.models.fv"
    assert [Path(p).resolve() for p in public.__path__] == [(_MODELS / "fv").resolve()]


def test_alias_enum_module_is_distinct_from_canonical_module() -> None:
    canonical = importlib.import_module("niwaki.models._generated.enums.FvBDType")
    public = importlib.import_module("niwaki.models.enums.FvBDType")

    assert public is not canonical
    assert public.__name__ == "niwaki.models.enums.FvBDType"


def test_class_identity_and_module_stay_canonical() -> None:
    """One class object, whose home is ``_generated`` whichever path imported it."""
    assert fvBD is _CanonicalFvBD
    assert fvBD.__module__ == "niwaki.models._generated.fv.fvBD"
    assert fvBD.__qualname__ == "fvBD"
    assert FvBDType is _CanonicalFvBDType
    assert FvBDType.__module__ == "niwaki.models._generated.enums.FvBDType"
    assert FvBDTypeFromAll is FvBDType is _CanonicalFvBDTypeFromAll


def test_registry_holds_the_one_class_object() -> None:
    assert REGISTRY["fvBD"] is fvBD


def test_deserialisation_returns_the_public_class() -> None:
    mo = ManagedObject.from_apic({"fvBD": {"attributes": {"name": "web", "dn": "uni/tn-a/BD-web"}}})
    assert type(mo) is fvBD
    assert isinstance(mo, _CanonicalFvBD)


def test_pickles_still_reference_the_canonical_module() -> None:
    """``__module__`` unchanged ⇒ pickles written before and after stay compatible."""
    assert pickle.loads(pickle.dumps(fvBD)) is fvBD
    payload = pickle.dumps(fvBD(name="web"))
    assert b"niwaki.models._generated.fv.fvBD" in payload
    assert b"niwaki.models.fv.fvBD" not in payload
    assert pickle.loads(payload) == fvBD(name="web")


# ── 5. Laziness ───────────────────────────────────────────────────────────────

_LOADED_PROBE = (
    "import sys\n"
    "{statement}\n"
    "watch = ('niwaki.models._generated.fv.', 'niwaki.models.fv.')\n"
    "print(','.join(sorted(m for m in sys.modules if m.startswith(watch))))\n"
)


def _loaded_fv_modules(statement: str) -> set[str]:
    completed = subprocess.run(
        [sys.executable, "-c", _LOADED_PROBE.format(statement=statement)],
        capture_output=True,
        cwd=_ROOT,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    out = completed.stdout.strip()
    return set(out.split(",")) if out else set()


def test_importing_a_mirror_package_loads_none_of_its_classes() -> None:
    loaded = _loaded_fv_modules("import niwaki.models.fv")
    assert not loaded, f"`import niwaki.models.fv` eagerly loaded: {sorted(loaded)}"


def test_short_name_access_loads_only_the_named_class() -> None:
    loaded = _loaded_fv_modules("import niwaki.models.fv as fv\nfv.BD")
    class_modules = {name.rsplit(".", 1)[1] for name in loaded}
    assert class_modules == {"fvBD"}, f"`fv.BD` loaded more than fvBD: {sorted(loaded)}"


def test_short_name_resolves_to_the_public_class() -> None:
    import niwaki.models.fv as fv

    assert fv.BD is fvBD
    assert fv.Tenant is importlib.import_module("niwaki.models.fv.fvTenant").fvTenant


# ── 6. Error paths ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["NoSuchClass", "bd", "fvNoSuchClass", "__wrapped__", ""])
def test_unknown_short_name_raises_attribute_error(name: str) -> None:
    """``__getattr__`` fails loud — and introspection probes see a plain miss."""
    import niwaki.models.fv as fv

    with pytest.raises(AttributeError) as excinfo:
        getattr(fv, name)
    assert repr(name) in str(excinfo.value)
    assert not hasattr(fv, name)


def test_unknown_short_name_error_names_the_public_module() -> None:
    import niwaki.models.fv as fv

    with pytest.raises(AttributeError, match=r"'niwaki\.models\.fv'"):
        fv.NoSuchClass  # noqa: B018 — the attribute access is the test


def test_from_import_of_unknown_short_name_raises_import_error() -> None:
    with pytest.raises(ImportError):
        exec("from niwaki.models.fv import NoSuchClass", {})


@pytest.mark.parametrize(
    "module",
    [
        "niwaki.models.fv.DoesNotExist",
        "niwaki.models.nosuchpkg",
        "niwaki.models.nosuchpkg.fvBD",
        "niwaki.models.enums.NoSuchEnum",
    ],
)
def test_unknown_submodule_raises_module_not_found(module: str) -> None:
    with pytest.raises(ModuleNotFoundError) as excinfo:
        importlib.import_module(module)
    assert excinfo.value.name is not None
    assert module.startswith(excinfo.value.name)


# ── 7. Static resolution (pyright, never called) ──────────────────────────────


def _static_model_import() -> None:
    assert_type(fvBD, type[_CanonicalFvBD])
    assert_type(fvBD(name="web"), _CanonicalFvBD)
    assert_type(fvBD(name="web").name, str)


def _static_enum_import() -> None:
    assert_type(FvBDType, type[_CanonicalFvBDType])
    assert_type(FvBDType("regular"), _CanonicalFvBDType)
    # Member access is narrowed to a literal by both pyright and mypy.
    assert_type(FvBDType.REGULAR, Literal[_CanonicalFvBDType.REGULAR])


def _static_enum_barrel_import() -> None:
    assert_type(FvBDTypeFromAll, type[_CanonicalFvBDType])
