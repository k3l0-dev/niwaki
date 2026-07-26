"""Parity guards between the generated-model set and its sibling artifacts.

Three surfaces each claim to know "the classes niwaki generates a typed model
for": the model files under ``models/_generated/``, the ``_PKG_MAP`` index
emitted alongside them, and ``domain._child_map.CLASS_PKG`` (the config/
navigation table).  They are produced by different generators with separately
maintained filters, and they HAVE drifted: CLASS_PKG once carried 18
deprecated classes with no model behind them (a reachable
``ModuleNotFoundError`` through facade jargon), and the shipped catalogue
once lacked 11 generated classes (``catalog.describe`` raised ``KeyError``
on classes the SDK ships models for).  These tests pin all the surfaces
against each other so neither gap can silently reopen.
"""

from __future__ import annotations

from pathlib import Path

from niwaki.domain._child_map import CLASS_PKG
from niwaki.models._generated import _PKG_MAP

_GEN_ROOT = Path("src/niwaki/models/_generated")


def _model_files() -> dict[str, str]:
    """{class name: package dir} for every generated model file on disk."""
    return {
        f.stem: f.parent.name
        for f in _GEN_ROOT.rglob("*.py")
        if not f.stem.startswith("_") and f.parent.name != "enums"
    }


def test_pkg_map_matches_the_model_files_exactly() -> None:
    """``_PKG_MAP`` is the codegen's own index of what it emitted — verify it."""
    files = _model_files()
    assert set(_PKG_MAP) == set(files), (
        f"_PKG_MAP drifted from the model tree: "
        f"map-only={sorted(set(_PKG_MAP) - set(files))[:5]}, "
        f"files-only={sorted(set(files) - set(_PKG_MAP))[:5]}. "
        f"Regenerate: uv run python -m niwaki._codegen.generate"
    )
    mismatched = {c for c, pkg in files.items() if _PKG_MAP[c] != pkg}
    assert not mismatched, f"package-dir mismatches: {sorted(mismatched)[:5]}"


def test_class_pkg_is_the_model_set_minus_fault_inst() -> None:
    """``CLASS_PKG`` == generated set minus {faultInst}, the one deliberate exception.

    ``faultInst`` has a generated model (faults are ack-able) but is excluded
    from the config/navigation table by ``moCategory=FaultCurrent`` — fault
    jargon must not appear on the design/facade surface.  Anything else in
    the diff is drift in ``generate_domain.py``'s filter.
    """
    expected = set(_PKG_MAP) - {"faultInst"}
    assert set(CLASS_PKG) == expected, (
        f"CLASS_PKG drifted from the generated set: "
        f"class_pkg-only={sorted(set(CLASS_PKG) - expected)[:5]}, "
        f"missing={sorted(expected - set(CLASS_PKG))[:5]}. "
        f"Regenerate: uv run python -m niwaki._codegen.generate_domain"
    )


def test_every_generated_class_resolves_in_the_shipped_catalogue() -> None:
    """``catalog.class_meta`` must never raise for a class the SDK ships a model for.

    The regression this pins: the catalogue build once skipped classes with an
    empty ``readAccess`` list, leaving 11 generated ``faultThrValue*`` classes
    unresolvable (``KeyError``) while their models shipped fine.
    """
    from niwaki import catalog

    unresolvable = []
    for name in sorted(_PKG_MAP):
        try:
            meta = catalog.class_meta(name)
        except KeyError:
            unresolvable.append(name)
        else:
            assert meta.has_model, f"{name} is generated but has_model is False"
    assert not unresolvable, (
        f"{len(unresolvable)} generated class(es) missing from the shipped "
        f"catalogue: {unresolvable[:12]}. "
        f"Regenerate: uv run python -m niwaki._codegen.generate_catalog"
    )


def test_generated_classes_are_concrete_and_non_stat_in_the_catalogue() -> None:
    """Every generated class is concrete and non-stat per the shipped catalogue."""
    from niwaki import catalog

    stat = [n for n in _PKG_MAP if catalog.class_meta(n).is_stat]
    assert not stat, f"generated classes flagged is_stat: {stat[:12]}"
    abstract = [n for n in _PKG_MAP if catalog.describe(n).is_abstract]
    assert not abstract, f"generated classes flagged is_abstract: {abstract[:12]}"


def test_shipped_catalogue_names_match_the_models() -> None:
    """Corpus-free naming parity against the SHIPPED catalog.db.

    The name_override table freezes the freezable divergences at build time;
    only the irreducible residue (model name collides with a catalogue-only
    wire prop) may differ.  This is the parity guarantee public CI enforces
    on the artifact users actually install.
    """
    from importlib import import_module

    from niwaki import catalog
    from niwaki.models._generated import _PKG_MAP

    irreducible = {
        ("vmmAgtStatus", "operSt"),
        ("vmmPlInf", "state"),
        ("vnsCMgmt", "gateway"),
        ("vnsCMgmt", "host"),
        ("vnsCMgmt", "subnetmask"),
    }
    divergences: set[tuple[str, str]] = set()
    for name in sorted(_PKG_MAP):
        model = getattr(import_module(f"niwaki.models._generated.{_PKG_MAP[name]}.{name}"), name)
        meta = catalog.class_meta(name)
        for field_name, field in model.model_fields.items():
            if field_name == "children":
                continue
            wire = field.serialization_alias or field_name
            catalogue_name = meta.wire_to_readable.get(wire)
            if catalogue_name is not None and catalogue_name != field_name:
                divergences.add((name, wire))
    assert divergences == irreducible
