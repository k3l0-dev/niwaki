"""Build ``migration_2_0.json`` — the 1.x → 2.0 rename table, by surface.

Dev-repo tool (needs the git history for the model surface).  Run from the
repo root::

    uv run python -m tools.niwaki_migrate.build_table

Surfaces:

- ``model_fields`` — readable field renames on the generated models (and
  therefore the typed cursor kwargs).  Ground truth: the generated model
  files of the last 1.x commit vs the working tree, introspected from the
  emitted ``Field(...)`` lines — never re-derived from policy.
- ``catalog_readable`` — readable-name renames the catalogue serves at
  runtime (operational props of generated classes plus every non-generated
  class).  Derived by running the frozen 1.x policy (``_legacy_naming``)
  and the live policy over the same catalogue inputs.
- ``navigation`` / ``makers`` — empty in 2.0, stated explicitly: neither
  surface was renamed by the wave.

The builder also emits ``maker_context`` (maker call-name → ACI classes it
creates, from the curated vocabulary) and ``current_names`` metadata the
codemod needs to decide which attribute renames are safe.
"""

from __future__ import annotations

import json
import keyword
import re
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from niwaki._schema.naming import propname_to_snake, resolve_py_names  # noqa: E402
from tools.niwaki_migrate import _legacy_naming  # noqa: E402

LAST_1X_COMMIT = "f81a960d"
DB = REPO / "src" / "niwaki" / "query" / "_catalog" / "catalog.db"
VOCABULARY = REPO / "src" / "niwaki" / "domain" / "vocabulary.yaml"
OUT = Path(__file__).resolve().parent / "migration_2_0.json"

# The six 1.x catalogue name_override rows (frozen model names the runtime
# applied on top of derivation) — part of the 1.x behavior being migrated.
_FROZEN_1X = {
    ("l3extMember", "addr"): "addr",
    ("l3extOut", "enforceRtctrl"): "enforce_rtctrl",
    ("l3extRsNodeL3OutAtt", "rtrId"): "rtr_id",
    ("l3extRsPathL3OutAtt", "addr"): "addr",
    ("l3extRsPathL3OutAtt", "llAddr"): "ll_addr",
    ("l3extRsPathL3OutAtt", "mac"): "mac",
}

_FIELD_LINE_RE = re.compile(r"^    ([a-z][a-z0-9_]*): ")
_ALIAS_RE = re.compile(r'serialization_alias="([^"]+)"')


def _model_fields_of(source: str) -> dict[str, str]:
    """``{wire_name: python_name}`` parsed from a generated model file.

    ``Field(...)`` declarations span several lines: the name sits on the
    declaration line, ``serialization_alias`` a few lines below.  A field
    with no alias serializes under its own name.
    """
    out: dict[str, str] = {}
    current: str | None = None
    for line in source.splitlines():
        m = _FIELD_LINE_RE.match(line)
        if m:
            current = m.group(1) if m.group(1) != "children" else None
            if current is not None:
                out[current] = current  # provisional: wire == py until an alias says otherwise
            alias = _ALIAS_RE.search(line)
            if current is not None and alias:
                out.pop(current)
                out[alias.group(1)] = current
                current = None
            continue
        alias = _ALIAS_RE.search(line)
        if current is not None and alias:
            out.pop(current)
            out[alias.group(1)] = current
            current = None
    return out


def _old_model_sources() -> dict[str, str]:
    """``{aci_class: file_source}`` of the last 1.x generated models."""
    listing = subprocess.run(
        [
            "git",
            "-C",
            str(REPO),
            "ls-tree",
            "-r",
            "--name-only",
            LAST_1X_COMMIT,
            "src/niwaki/models/_generated",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    sources: dict[str, str] = {}
    for path in listing:
        stem = Path(path).stem
        if stem.startswith("_") or Path(path).parent.name == "enums":
            continue
        sources[stem] = subprocess.run(
            ["git", "-C", str(REPO), "show", f"{LAST_1X_COMMIT}:{path}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    return sources


def _build_model_surface() -> tuple[dict[str, dict[str, dict[str, str]]], set[str]]:
    """Return (surface, every CURRENT model field name across all classes)."""
    old_sources = _old_model_sources()
    surface: dict[str, dict[str, dict[str, str]]] = {}
    current_model_names: set[str] = set()
    gen_root = REPO / "src" / "niwaki" / "models" / "_generated"
    for f in sorted(gen_root.rglob("*.py")):
        if f.stem.startswith("_") or f.parent.name == "enums":
            continue
        cls = f.stem
        new_fields = _model_fields_of(f.read_text())
        current_model_names.update(new_fields.values())
        old_src = old_sources.get(cls)
        if old_src is None:
            continue
        old_fields = _model_fields_of(old_src)
        renames = {
            wire: {"old": old_fields[wire], "new": new_fields[wire]}
            for wire in new_fields
            if wire in old_fields and old_fields[wire] != new_fields[wire]
        }
        if renames:
            surface[cls] = renames
    return surface, current_model_names


def _build_catalog_surface(
    model_surface: dict[str, dict[str, dict[str, str]]],
) -> dict[str, dict[str, dict[str, str]]]:
    con = sqlite3.connect(str(DB))
    labels = dict(con.execute("SELECT id, text FROM label_pool"))
    names = dict(con.execute("SELECT id, class_name FROM mo"))
    flag_order = dict(con.execute("SELECT key, value FROM manifest"))["prop_flags"].split(",")
    naming_bit = 1 << flag_order.index("isNaming")

    props: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    for class_id, wire, label_id, flags in con.execute(
        "SELECT class_id, wire_name, label_id, flags FROM prop"
    ):
        props[class_id][wire] = {
            "label": labels.get(label_id, "") if label_id is not None else "",
            "is_naming": bool(flags & naming_bit),
        }
    sm: dict[int, dict[str, str]] = defaultdict(dict)
    for class_id, wire, sm_label in con.execute(
        "SELECT class_id, wire_name, sm_label FROM scopemeta"
    ):
        sm[class_id][wire] = sm_label
    con.close()

    surface: dict[str, dict[str, dict[str, str]]] = {}
    current_names: set[str] = set()
    for class_id, shaped in props.items():
        cls = names[class_id]
        sm_class = sm.get(class_id, {})
        new_failed = old_failed = False
        try:
            new_names = resolve_py_names(shaped, sm_class, cls)
        except ValueError:
            new_failed = True
            new_names = {w: propname_to_snake(w) for w in shaped}
        try:
            old_names = _legacy_naming.resolve_py_names(shaped, sm_class, cls)
        except ValueError:
            old_failed = True
            old_names = {w: propname_to_snake(w) for w in shaped}
        if new_failed != old_failed:
            # One policy resolves where the other cannot: emitting "no renames"
            # here would silently drop this class from the table.
            raise RuntimeError(
                f"{cls}: naming resolution failed under "
                f"{'the live' if new_failed else 'the legacy'} policy only - "
                "the table cannot represent this class; investigate."
            )
        for (c2, wire), pin in _FROZEN_1X.items():
            if c2 == cls and wire in old_names:
                old_names[wire] = pin
        current_names.update(new_names.values())
        model_wires = set(model_surface.get(cls, {}))
        renames = {
            w: {"old": old_names[w], "new": new_names[w]}
            for w in new_names
            if old_names.get(w) != new_names[w] and w not in model_wires
        }
        if renames:
            surface[cls] = renames
    _build_catalog_surface.current_names = current_names  # type: ignore[attr-defined]
    return surface


def _build_maker_context() -> dict[str, list[str]]:
    vocab = yaml.safe_load(VOCABULARY.read_text())
    context: dict[str, set[str]] = defaultdict(set)
    for children in vocab.get("makers", {}).values():
        for maker, target in (children or {}).items():
            if isinstance(target, str):
                context[maker].add(target)
    return {m: sorted(cs) for m, cs in sorted(context.items())}


def main() -> None:
    model_surface, current_model_names = _build_model_surface()
    catalog_surface = _build_catalog_surface(model_surface)
    maker_context = _build_maker_context()
    current_names: set[str] = _build_catalog_surface.current_names  # type: ignore[attr-defined]

    n_model = sum(len(v) for v in model_surface.values())
    n_cat = sum(len(v) for v in catalog_surface.values())

    # Safe-to-rewrite attribute renames: the old name maps to ONE new name
    # across every surface AND no class still serves the old name today.
    old_to_new: dict[str, set[str]] = defaultdict(set)
    for surface in (model_surface, catalog_surface):
        for renames in surface.values():
            for entry in renames.values():
                old_to_new[entry["old"]].add(entry["new"])
    # Rewrite candidates need uniqueness AND death AND distinctiveness:
    # a >=4-token sentence-name cannot plausibly exist on a foreign object,
    # a 2-3-token one (managed_by, mounted_on, date_and_time) can - those
    # are never rewritten (and the short ones not even flagged; documented).
    attribute_safe = {
        old: nexts.pop()
        for old, nexts in ((o, set(n)) for o, n in old_to_new.items())
        if len(nexts) == 1
        and old not in current_names
        and not keyword.iskeyword(old)
        and old.count("_") >= 3
    }
    # Flag-only set: any old name distinctive enough (>=4 tokens) that is NOT
    # safely rewritable - multi-target, still-current somewhere, or simply
    # attribute-read surface the tool refuses to touch blindly.
    attribute_flag = sorted(
        o
        for o in old_to_new
        if o.count("_") >= 3 and o not in attribute_safe and not keyword.iskeyword(o)
    )
    # Old MODEL-surface names no generated model serves anymore — safe to
    # *flag* in keyword/key positions (kwargs of unknown calls, dict keys,
    # subscripts): keyword positions target models and makers, so the
    # catalogue's operational mirror classes (whose wire fallback can keep an
    # old spelling alive, e.g. enforce_rtctrl on l3extOutDef) do not make the
    # name legitimate there.
    old_model_names = {e["old"] for renames in model_surface.values() for e in renames.values()}
    retired_kwarg_names = sorted(old_model_names - current_model_names)

    table = {
        "schema_version": 1,
        "niwaki_from": "1.10.0",
        "niwaki_to": "2.0.0",
        "surfaces": {
            "model_fields": {c: model_surface[c] for c in sorted(model_surface)},
            "catalog_readable": {c: catalog_surface[c] for c in sorted(catalog_surface)},
            "navigation": {},
            "makers": {},
        },
        "maker_context": maker_context,
        "attribute_safe": dict(sorted(attribute_safe.items())),
        "attribute_flag": attribute_flag,
        "retired_kwarg_names": retired_kwarg_names,
        "counts": {
            "model_fields": n_model,
            "catalog_readable": n_cat,
            "attribute_safe": len(attribute_safe),
            "attribute_flag": len(attribute_flag),
            "retired_kwarg_names": len(retired_kwarg_names),
        },
    }
    OUT.write_text(json.dumps(table, indent=1, sort_keys=False) + "\n")
    print(
        f"wrote {OUT.name}: model_fields={n_model} catalog_readable={n_cat} "
        f"attribute_safe={len(attribute_safe)} makers={len(maker_context)}"
    )


if __name__ == "__main__":
    main()
