"""Build the read catalogue — a shipped sqlite metadata store for every ACI class.

The query builder can read all ~15,300 *readable* ACI classes by name, but only
the 2,239 it generates as Pydantic models carry readable field names, enums and
relations.  The catalogue closes that gap for the other ~13,000 (learned
endpoints, stats, hardware, routing runtime): a compact, shipped ``.db`` of read
metadata, opened lazily, never touched at import.

**Lossless by construction, small by decision.**  A naive verbatim-per-class
capture measures 87 MB (Lot 0) — above PyPI's per-file ceiling and too heavy to
ship.  So every schema field is *routed*: lifted into a queryable column/table,
interned into a pool, or kept verbatim in a per-row ``residual`` blob.  A handful
are *dropped* — each an explicit decision with a written reason, never a silent
default (the discipline of :mod:`niwaki._codegen.basetypes`, where an unknown
family breaks the build rather than degrading quietly).  Purely *derived* fields
(``py_name``, ``kind``) are not stored at all — they are recomputed on read from
the fields they derive from, so the catalogue keeps one source of truth.

Two guarantees make "lossless" a fact rather than a hope:

* **Coverage (fail-loud).**  Every top-level key of every class and property must
  be a known routed key or an explicit dropped key; anything else raises
  :class:`UnroutedSchemaKeyError`.  A firmware that adds a field breaks *this*
  build, not a user's code.
* **Reconstruction.**  :func:`reconstruct_class` rebuilds a class's schema dict
  from the catalogue; the test suite asserts it equals the original modulo the
  dropped keys — proof that nothing is lost silently.

Run::

    uv run python -m niwaki._codegen.generate_catalog [out.db]
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import zlib
from pathlib import Path
from typing import Any

from niwaki._codegen.basetypes import kind_value_or_none

_DATA = Path(__file__).resolve().parents[3] / "data"
SCHEMA_DIR = _DATA / "schemas" / "mo-apic-v6.0_9c"
SCOPEMETA_PATH = _DATA / "extracted" / "scopemeta_labels.json"
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "query" / "_catalog" / "catalog.db"
APIC_VERSION = "6.0(9c)"


class UnroutedSchemaKeyError(KeyError):
    """A schema key is neither routed to storage nor explicitly dropped.

    Raised at build time.  Fatal by design: the alternative is to sweep the key
    into a catch-all and lose track of it, the silent degradation the routing
    table exists to prevent.
    """


# ── Class-level routing (15,301 rows — kept as plain columns) ─────────────────
CLASS_TEXT_COLS: dict[str, str] = {
    "classPkg": "class_pkg",
    "classId": "class_id",
    "rnFormat": "rn_format",
    "moCategory": "mo_category",
    "abstractionLayer": "abstraction_layer",
    "healthCollectionSource": "health_collection_source",
    "monitoringPolicySource": "monitoring_policy_source",
    "isCreatableDeletable": "is_creatable_deletable",
    "featureTag": "feature_tag",
}
CLASS_FLAG_COLS: dict[str, str] = {
    "isAbstract": "is_abstract",
    "isConfigurable": "is_configurable",
    "isContextRoot": "is_context_root",
    "isNxosConverged": "is_nxos_converged",
    "isDeprecated": "is_deprecated",
    "isHidden": "is_hidden",
    "isEncrypted": "is_encrypted",
    "isExportable": "is_exportable",
    "isPersistent": "is_persistent",
    "isSubjectToQuota": "is_subject_to_quota",
    "isObservable": "is_observable",
    "hasStats": "has_stats",
    "isStat": "is_stat",
    "isFaultable": "is_faultable",
    "isDomainable": "is_domainable",
    "isHealthScorable": "is_health_scorable",
    "shouldCollectHealthStats": "should_collect_health_stats",
    "hasEventRules": "has_event_rules",
    "apicNxProcessing": "apic_nx_processing",
}
# Lifted into their own queryable tables and reconstructed from them (the proof
# of losslessness passes through the tables, exactly like ``properties``).
CLASS_NORMALIZED = {"properties", "superClasses", "subClasses", "faults"}
# ``className`` from the primary key; ``label``/``comment`` pool; ``identifiedBy``
# its own ``identified_by`` column (reconstructed from it, not double-stored).
CLASS_HANDLED = {"className", "label", "comment", "identifiedBy"}
CLASS_DROPPED: dict[str, str] = {
    "dnFormats": "DN-parsing template; no read-catalogue consumer (45.7 MB verbatim)",
    "rnMap": "RN-parsing map; derivable from each child's rnFormat",
}
CLASS_RESIDUAL_KEYS = {
    # ``contains`` (child classes) stays here: it is highly repetitive, so the
    # zlib residual (~0.5 MB) crushes it far below an uncompressed table (~2.5 MB);
    # ``describe`` reads it from the decompressed residual (a cold path).
    "contains",
    "containedBy",
    "relationTo",
    "relationFrom",
    "relationInfo",  # also indexed in the ``relation`` table (tiny → kept raw here)
    "readAccess",
    "writeAccess",
    "stats",
    "statsGroup",
    "events",
    "platformFlavors",
}

# ── Property-level routing (330,786 rows — the bulk; interned aggressively) ────
# Type strings interned into a shared pool (int FK): 80 baseType / 3403 modelType
# / 8 uitype distinct across 330k props — storing the string each time was 10 MB.
PROP_TYPE_COLS: dict[str, str] = {
    "baseType": "base_type_id",
    "modelType": "model_type_id",
    "uitype": "uitype_id",
}
PROP_TEXT_COLS: dict[str, str] = {
    "default": "default_val",
    "likeProp": "like_prop",
}
# All 14 present on every property (verified) → packed into one int bitmask.
PROP_FLAG_ORDER: tuple[str, ...] = (
    "isConfigurable",
    "needsPropDelimiters",
    "createOnly",
    "readWrite",
    "readOnly",
    "isNaming",
    "secure",
    "implicit",
    "mandatory",
    "isOverride",
    "isLike",
    "isNxosConverged",
    "isDeprecated",
    "isHidden",
)
PROP_HANDLED = {"label", "comment", "validValues"}
PROP_DROPPED: dict[str, str] = {
    "propGlobalId": "internal APIC property id; no read-catalogue consumer",
    "propLocalId": "internal APIC property id; no read-catalogue consumer",
}
PROP_RESIDUAL_KEYS = {
    "validators",
    "platformFlavors",
    "validateAsIPv4OrIPv6",
    "validateAsMAC",
    "validateAsIPv4",
    "validateAsIPv6",
}


def _compact(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode()


def _zip(obj: Any) -> bytes:
    return zlib.compress(_compact(obj), 9)


def _unzip(blob: bytes) -> Any:
    return json.loads(zlib.decompress(blob))


def load_class(path: Path) -> tuple[str, dict[str, Any]] | None:
    """Return ``(wire_class_name, class_dict)`` for a schema file, or ``None``."""
    try:
        doc = json.loads(path.read_bytes())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(doc, dict) or len(doc) != 1:
        return None
    cls = next(iter(doc.values()))
    if not isinstance(cls, dict):
        return None
    pkg, name = cls.get("classPkg"), cls.get("className")
    if not (pkg and name):
        return None
    return f"{pkg}{name}", cls


class _Pool:
    """A string-interning table: identical text stored once, keyed by int id."""

    def __init__(self) -> None:
        self._ids: dict[str, int] = {}

    def intern(self, text: str) -> int:
        got = self._ids.get(text)
        if got is None:
            got = self._ids[text] = len(self._ids)
        return got

    def rows(self) -> list[tuple[int, str]]:
        return [(i, t) for t, i in self._ids.items()]


class _EnumStore:
    """Deduplicated validValues store, keyed by content hash, addressed by int."""

    def __init__(self) -> None:
        self._by_hash: dict[str, int] = {}
        self._blobs: list[bytes] = []

    def add(self, values: list[Any]) -> int:
        digest = hashlib.sha1(_compact(values)).hexdigest()
        got = self._by_hash.get(digest)
        if got is None:
            got = self._by_hash[digest] = len(self._blobs)
            self._blobs.append(_zip(values))
        return got

    def rows(self) -> list[tuple[int, bytes]]:
        return list(enumerate(self._blobs))


def _pack_flags(prop: dict[str, Any], class_name: str, wire: str) -> int:
    bits = 0
    for i, flag in enumerate(PROP_FLAG_ORDER):
        if flag not in prop:
            raise UnroutedSchemaKeyError(
                f"{class_name}.{wire}: property flag {flag!r} absent — the bitmask "
                "assumes all flags are present (verified across the 6.0 corpus). "
                "Route it as a nullable column instead."
            )
        if prop[flag]:
            bits |= 1 << i
    return bits


def _prop_row(
    class_id: int,
    class_name: str,
    wire: str,
    prop: dict[str, Any],
    labels: _Pool,
    comments: _Pool,
    types: _Pool,
    enums: _EnumStore,
    unknown_types: set[str],
) -> tuple[Any, ...]:
    """Route one property into a prop-table row tuple. Fail-loud on unknown keys."""
    base_type = str(prop.get("baseType", ""))
    if base_type and kind_value_or_none(base_type) is None:
        unknown_types.add(base_type)  # a family beyond the model set — reported, not swallowed

    enum_id: int | None = None
    vv = prop.get("validValues")
    # Keep real enums / scalar dicts (deduped); drop the mo:* identifier registers
    # (redundant with the mo/prop tables — a monument to a misreading of the schema).
    if isinstance(vv, list) and vv and not base_type.startswith("mo:"):
        enum_id = enums.add(vv)

    # ``platformFlavors`` is present on every property but empty on all but 88
    # (platform-specific overrides).  Storing ``[]`` 330k times cost 10 MB; it is
    # the universal default, so an empty one is not stored and is reconstructed.
    residual = {
        k: v
        for k, v in prop.items()
        if k in PROP_RESIDUAL_KEYS and not (k == "platformFlavors" and not v)
    }
    routed = (
        set(PROP_TYPE_COLS)
        | set(PROP_TEXT_COLS)
        | set(PROP_FLAG_ORDER)
        | PROP_HANDLED
        | set(PROP_DROPPED)
        | PROP_RESIDUAL_KEYS
    )
    unknown = set(prop) - routed
    if unknown:
        raise UnroutedSchemaKeyError(
            f"{class_name}.{wire}: unrouted property key(s) {sorted(unknown)}. "
            "Route them (column/pool/residual) or drop them with a reason."
        )
    return (
        class_id,
        wire,
        labels.intern(str(prop["label"])) if "label" in prop else None,
        comments.intern(json.dumps(prop["comment"])) if "comment" in prop else None,
        enum_id,
        types.intern(base_type) if "baseType" in prop else None,
        types.intern(str(prop["modelType"])) if "modelType" in prop else None,
        types.intern(str(prop["uitype"])) if "uitype" in prop else None,
        json.dumps(prop["default"]) if "default" in prop else None,
        str(prop["likeProp"]) if "likeProp" in prop else None,
        _pack_flags(prop, class_name, wire),
        _zip(residual) if residual else None,
    )


def build_catalog(schema_dir: Path = SCHEMA_DIR, out: Path = DEFAULT_OUT) -> dict[str, int]:
    """Build the catalogue ``.db`` from the raw schemas; return build stats.

    Raises:
        UnroutedSchemaKeyError: a schema key is neither routed nor dropped.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    con = sqlite3.connect(out)
    _create_schema(con)

    labels, comments, types = _Pool(), _Pool(), _Pool()
    enums = _EnumStore()
    unknown_types: set[str] = set()
    mo_rows: list[tuple[Any, ...]] = []
    prop_rows: list[tuple[Any, ...]] = []
    inherits: list[tuple[int, str]] = []
    subclass: list[tuple[int, str]] = []
    faults: list[tuple[int, str, str]] = []
    relations: list[tuple[Any, ...]] = []
    scopemeta_rows: list[tuple[int, str, str]] = []
    search_docs: list[tuple[int, str]] = []
    scopemeta = json.loads(SCOPEMETA_PATH.read_text()) if SCOPEMETA_PATH.exists() else {}
    n_props = 0
    mo_cols = _mo_column_order()

    class_id = 0
    for path in sorted(schema_dir.glob("*.json")):
        loaded = load_class(path)
        if loaded is None:
            continue
        name, cls = loaded
        if not cls.get("readAccess"):  # readable classes only
            continue
        _check_class_coverage(name, cls)

        mo_rows.append(_mo_row(class_id, name, cls, labels, comments, mo_cols))
        for wire, prop in (cls.get("properties") or {}).items():
            if not isinstance(prop, dict):
                continue
            prop_rows.append(
                _prop_row(
                    class_id,
                    name,
                    wire,
                    prop,
                    labels,
                    comments,
                    types,
                    enums,
                    unknown_types,
                )
            )
            n_props += 1
        inherits += [(class_id, a) for a in cls.get("superClasses") or []]
        subclass += [(class_id, c) for c in cls.get("subClasses") or {}]
        faults += _fault_rows(class_id, name, cls.get("faults"))
        relations += _relation_row(name, cls.get("relationInfo"))
        scopemeta_rows += _scopemeta_rows(class_id, cls, scopemeta)
        search_docs.append((class_id, _search_text(name, cls)))
        class_id += 1

    con.executemany(f"INSERT INTO mo VALUES ({_ph(len(mo_cols))})", mo_rows)
    con.executemany(f"INSERT INTO prop VALUES ({_ph(len(_prop_column_order()))})", prop_rows)
    con.executemany("INSERT INTO label_pool VALUES (?,?)", labels.rows())
    con.executemany("INSERT INTO comment_pool VALUES (?,?)", comments.rows())
    con.executemany("INSERT INTO type_pool VALUES (?,?)", types.rows())
    con.executemany("INSERT INTO enum VALUES (?,?)", enums.rows())
    con.executemany("INSERT INTO inherits VALUES (?,?)", inherits)
    con.executemany("INSERT INTO subclass VALUES (?,?)", subclass)
    con.executemany("INSERT INTO fault VALUES (?,?,?)", faults)
    con.executemany(f"INSERT INTO relation VALUES ({_ph(9)})", relations)
    con.executemany("INSERT INTO scopemeta VALUES (?,?,?)", scopemeta_rows)
    con.executemany("INSERT INTO search_doc VALUES (?,?)", search_docs)
    con.execute("INSERT INTO catalog_fts(catalog_fts) VALUES('rebuild')")

    stats = {
        "classes": len(mo_rows),
        "properties": n_props,
        "enums": len(enums.rows()),
        "labels": len(labels.rows()),
        "comments": len(comments.rows()),
        "types": len(types.rows()),
        "relations": len(relations),
        "scopemeta": len(scopemeta_rows),
        "unclassified_basetypes": len(unknown_types),
    }
    # A content digest of the class set + counts — a real fingerprint the freshness
    # guard compares (two different corpora with identical counts differ here).
    content_hash = hashlib.sha1(
        ("\n".join(sorted(str(row[1]) for row in mo_rows)) + _compact(stats).decode()).encode()
    ).hexdigest()
    _write_manifest(con, stats, unknown_types, content_hash)
    con.commit()
    con.execute("VACUUM")
    con.close()
    stats["size_bytes"] = out.stat().st_size
    return stats


def _ph(n: int) -> str:
    return ",".join("?" * n)


def _check_class_coverage(name: str, cls: dict[str, Any]) -> None:
    known = (
        set(CLASS_TEXT_COLS)
        | set(CLASS_FLAG_COLS)
        | CLASS_NORMALIZED
        | CLASS_HANDLED
        | set(CLASS_DROPPED)
        | CLASS_RESIDUAL_KEYS
    )
    unknown = set(cls) - known
    if unknown:
        raise UnroutedSchemaKeyError(
            f"{name}: unrouted class key(s) {sorted(unknown)}. Route or drop them."
        )


def _mo_column_order() -> list[str]:
    return [
        "id",
        "class_name",
        "short_name",
        "label_id",
        "comment_id",
        "identified_by",
        *CLASS_TEXT_COLS.values(),
        *CLASS_FLAG_COLS.values(),
        "residual",
    ]


def _mo_row(
    class_id: int,
    name: str,
    cls: dict[str, Any],
    labels: _Pool,
    comments: _Pool,
    cols: list[str],
) -> tuple[Any, ...]:
    row: dict[str, Any] = {
        "id": class_id,
        "class_name": name,
        "short_name": cls["className"],
        "label_id": labels.intern(str(cls["label"])) if "label" in cls else None,
        "comment_id": (comments.intern(json.dumps(cls["comment"])) if "comment" in cls else None),
        "identified_by": (json.dumps(cls["identifiedBy"]) if "identifiedBy" in cls else None),
    }
    for wk, col in CLASS_TEXT_COLS.items():
        row[col] = str(cls[wk]) if wk in cls else None
    for wk, col in CLASS_FLAG_COLS.items():
        row[col] = int(bool(cls[wk])) if wk in cls else None
    residual = {
        k: v
        for k, v in cls.items()
        if k in CLASS_RESIDUAL_KEYS and not (k == "platformFlavors" and not v)
    }
    row["residual"] = _zip(residual) if residual else None
    return tuple(row[c] for c in cols)


def _fault_rows(class_id: int, name: str, faults: Any) -> list[tuple[int, str, str]]:
    if not isinstance(faults, dict):
        return []
    out: list[tuple[int, str, str]] = []
    for code, info in faults.items():
        # 6.0(9c): a fault value is always ``{code: rule_name}`` (a str).  The table
        # stores only (code, name); a richer value would be silently lost, and the
        # class/prop-key coverage guard would not see it — so fail loud here instead.
        if not isinstance(info, str):
            raise UnroutedSchemaKeyError(
                f"{name}: faults[{code!r}] is {type(info).__name__}, not a str — the "
                "fault table stores only (code, name); route the richer value first."
            )
        out.append((class_id, str(code), info))
    return out


def _search_text(name: str, cls: dict[str, Any]) -> str:
    """The searchable document for a class: its wire name and GUI label.

    Kept to the two strongest, cheapest discovery signals.  Folding in comments
    would re-store the ``comment_pool`` text (a second copy the FTS index would
    grow again → +3.4 MB); folding in 330k prop labels would triple the file.
    Property search (``find_prop`` — "which class carries a MAC?") queries the
    ``prop`` table directly; comment search can scan ``comment_pool`` on demand.
    """
    return f"{name} {cls.get('label', '')}".strip()


def _relation_row(name: str, info: Any) -> list[tuple[Any, ...]]:
    """One ``relation`` row from an Rs class's relationInfo (empty for non-Rs)."""
    if not isinstance(info, dict):
        return []
    return [
        (
            name,
            info.get("type"),
            info.get("cardinality"),
            info.get("fromMo"),
            info.get("fromRelMo"),
            info.get("toMo"),
            info.get("toRelMo"),
            int(bool(info.get("enforceable"))),
            int(bool(info.get("resolvable"))),
        )
    ]


def _scopemeta_rows(
    class_id: int, cls: dict[str, Any], scopemeta: dict[str, dict[str, str]]
) -> list[tuple[int, str, str]]:
    """Scopemeta property labels (``{prop: sm_label}``) for the 658 classes that have them.

    Not part of the schema — added metadata that lets the runtime reproduce the
    generator's ``resolve_py_names`` exactly (same inputs), so a catalogue-served
    class and its generated model agree on field names by construction.
    """
    dotted = f"{cls['classPkg']}.{cls['className']}"
    labels = scopemeta.get(dotted)
    if not labels:
        return []
    return [(class_id, wire, label) for wire, label in labels.items()]


def _write_manifest(
    con: sqlite3.Connection,
    stats: dict[str, int],
    unknown_types: set[str],
    content_hash: str,
) -> None:
    rows = [("apic_version", APIC_VERSION), ("content_hash", content_hash)]
    rows += [(k, str(v)) for k, v in stats.items()]
    # The prop.flags bitmask layout, so the runtime unpacks it without importing
    # this generator (the .db is self-describing).
    rows.append(("prop_flags", ",".join(PROP_FLAG_ORDER)))
    # Read-only families we could not classify — visible, not swallowed.
    rows.append(("unclassified_basetype_list", ",".join(sorted(unknown_types))))
    con.executemany("INSERT INTO manifest VALUES (?,?)", rows)


def _create_schema(con: sqlite3.Connection) -> None:
    text = ", ".join(f"{c} TEXT" for c in CLASS_TEXT_COLS.values())
    flags = ", ".join(f"{c} INT" for c in CLASS_FLAG_COLS.values())
    con.executescript(f"""
        CREATE TABLE mo(
          id INTEGER PRIMARY KEY, class_name TEXT UNIQUE NOT NULL, short_name TEXT,
          label_id INT, comment_id INT, identified_by TEXT,
          {text}, {flags}, residual BLOB
        );
        CREATE TABLE prop(
          class_id INT, wire_name TEXT, label_id INT, comment_id INT, enum_id INT,
          base_type_id INT, model_type_id INT, uitype_id INT,
          default_val TEXT, like_prop TEXT, flags INT, residual BLOB,
          PRIMARY KEY(class_id, wire_name)
        ) WITHOUT ROWID;
        CREATE TABLE label_pool(id INT PRIMARY KEY, text TEXT) WITHOUT ROWID;
        CREATE TABLE comment_pool(id INT PRIMARY KEY, text TEXT) WITHOUT ROWID;
        CREATE TABLE type_pool(id INT PRIMARY KEY, value TEXT) WITHOUT ROWID;
        CREATE TABLE enum(id INT PRIMARY KEY, content BLOB) WITHOUT ROWID;
        CREATE TABLE inherits(class_id INT, ancestor TEXT);
        CREATE TABLE subclass(class_id INT, subclass TEXT);
        CREATE TABLE fault(class_id INT, code TEXT, name TEXT);
        CREATE TABLE relation(
          rs_class TEXT PRIMARY KEY, rel_type TEXT, cardinality TEXT,
          from_mo TEXT, from_rel_mo TEXT, to_mo TEXT, to_rel_mo TEXT,
          enforceable INT, resolvable INT
        ) WITHOUT ROWID;
        CREATE TABLE scopemeta(class_id INT, wire_name TEXT, sm_label TEXT);
        -- Discovery: one searchable doc per class (wire name + GUI label).  The
        -- plain table serves the LIKE fallback; catalog_fts is an external-content
        -- FTS5 index over it (no second copy of the text).
        CREATE TABLE search_doc(class_id INTEGER PRIMARY KEY, text TEXT);
        CREATE VIRTUAL TABLE catalog_fts USING fts5(
          text, content='search_doc', content_rowid='class_id'
        );
        CREATE TABLE manifest(key TEXT PRIMARY KEY, value TEXT);
        CREATE INDEX ix_inherits ON inherits(class_id);
        CREATE INDEX ix_subclass ON subclass(class_id);
        CREATE INDEX ix_fault ON fault(class_id);
        CREATE INDEX ix_scopemeta ON scopemeta(class_id);
        CREATE INDEX ix_relation_from ON relation(from_mo);
        CREATE INDEX ix_relation_to ON relation(to_mo);
    """)


def reconstruct_class(con: sqlite3.Connection, class_name: str) -> dict[str, Any]:
    """Rebuild a class's original schema dict from the catalogue (modulo dropped keys).

    The inverse of the build routing.  The reconstruction test asserts this equals
    the source schema with :data:`CLASS_DROPPED`, :data:`PROP_DROPPED` and the
    ``mo:*`` identifier ``validValues`` removed — the proof that routing loses
    nothing silently.  Derived fields (``py_name``, ``kind``) are recomputed by
    the runtime, not stored, so they never appear here.
    """
    mo_cols = _mo_column_order()
    row = con.execute("SELECT * FROM mo WHERE class_name=?", (class_name,)).fetchone()
    if row is None:
        raise KeyError(class_name)
    r = dict(zip(mo_cols, row, strict=True))
    cid = r["id"]
    out: dict[str, Any] = {"className": r["short_name"], "classPkg": r["class_pkg"]}
    for wk, col in CLASS_TEXT_COLS.items():
        if r[col] is not None:
            out[wk] = r[col]
    for wk, col in CLASS_FLAG_COLS.items():
        if r[col] is not None:
            out[wk] = bool(r[col])
    if r["label_id"] is not None:
        out["label"] = _pool_get(con, "label_pool", r["label_id"])
    if r["comment_id"] is not None:
        out["comment"] = json.loads(_pool_get(con, "comment_pool", r["comment_id"]))
    if r["identified_by"] is not None:
        out["identifiedBy"] = json.loads(r["identified_by"])
    if r["residual"] is not None:
        out.update(_unzip(r["residual"]))
    out.setdefault("platformFlavors", [])  # universal default; empty ones not stored

    # These four keys are universal across the corpus, so always emitted (even
    # empty).  ``superClasses`` is a list — order is preserved via rowid; the
    # others are order-independent dicts.
    out["properties"] = dict(_reconstruct_props(con, cid))
    out["superClasses"] = [
        a
        for (a,) in con.execute(
            "SELECT ancestor FROM inherits WHERE class_id=? ORDER BY rowid", (cid,)
        )
    ]
    out["subClasses"] = {
        s: "" for (s,) in con.execute("SELECT subclass FROM subclass WHERE class_id=?", (cid,))
    }
    out["faults"] = dict(
        con.execute("SELECT code, name FROM fault WHERE class_id=?", (cid,)).fetchall()
    )
    return out


def _prop_column_order() -> list[str]:
    """The ``prop`` table columns, in insert/select order (one source for both)."""
    return [
        "class_id",
        "wire_name",
        "label_id",
        "comment_id",
        "enum_id",
        "base_type_id",
        "model_type_id",
        "uitype_id",
        "default_val",
        "like_prop",
        "flags",
        "residual",
    ]


def _reconstruct_props(con: sqlite3.Connection, class_id: int) -> list[tuple[str, dict[str, Any]]]:
    rows = con.execute("SELECT * FROM prop WHERE class_id=?", (class_id,)).fetchall()
    cols = _prop_column_order()
    out: list[tuple[str, dict[str, Any]]] = []
    for row in rows:
        r = dict(zip(cols, row, strict=True))
        p: dict[str, Any] = {}
        if r["residual"] is not None:
            p.update(_unzip(r["residual"]))
        if r["base_type_id"] is not None:
            p["baseType"] = _pool_get(con, "type_pool", r["base_type_id"])
        if r["model_type_id"] is not None:
            p["modelType"] = _pool_get(con, "type_pool", r["model_type_id"])
        if r["uitype_id"] is not None:
            p["uitype"] = _pool_get(con, "type_pool", r["uitype_id"])
        if r["default_val"] is not None:
            p["default"] = json.loads(r["default_val"])  # preserves int/str/bool
        if r["like_prop"] is not None:
            p["likeProp"] = r["like_prop"]
        for i, flag in enumerate(PROP_FLAG_ORDER):
            p[flag] = bool(r["flags"] & (1 << i))
        p.setdefault("platformFlavors", [])  # universal default; empty ones not stored
        if r["label_id"] is not None:
            p["label"] = _pool_get(con, "label_pool", r["label_id"])
        if r["comment_id"] is not None:
            p["comment"] = json.loads(_pool_get(con, "comment_pool", r["comment_id"]))
        if r["enum_id"] is not None:
            (blob,) = con.execute("SELECT content FROM enum WHERE id=?", (r["enum_id"],)).fetchone()
            p["validValues"] = _unzip(blob)
        out.append((r["wire_name"], p))
    return out


def _pool_get(con: sqlite3.Connection, table: str, pool_id: int) -> str:
    col = "value" if table == "type_pool" else "text"
    # ``table`` is one of two internal constants, never user input.
    (text,) = con.execute(f"SELECT {col} FROM {table} WHERE id=?", (pool_id,)).fetchone()
    return str(text)


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    stats = build_catalog(out=out)
    print(f"catalogue built: {out}")
    for k, v in stats.items():
        print(f"  {k:22} : {v}")
    print(f"  size                   : {stats['size_bytes'] / (1024 * 1024):.1f} MB")


if __name__ == "__main__":
    main()
