"""Runtime access to the read catalogue — the lazy, cold-start-safe reader.

The catalogue ``.db`` (built by :mod:`niwaki._codegen.generate_catalog`) holds
read metadata for every readable ACI class.  This module opens it **lazily** —
nothing here runs at ``import niwaki``; the connection is made on the first
lookup — and memoises a :class:`ClassMeta` per class so a query that returns
thousands of objects of one class touches sqlite once, then serves dict hits.

**Naming parity, by construction (with one measured caveat).**  The readable
field names are recomputed here with the *same* function the code generator uses
(:func:`niwaki._codegen._label_utils.resolve_py_names`) fed the *same* inputs —
the property labels and the scopemeta labels the catalogue stores — so a class
the catalogue serves dynamically reads with the field names its generated model
would use.

The caveat: ``resolve_py_names`` resolves name collisions over *all* of a class's
readable properties here, but over the smaller *configurable* subset in the
generator, so the two can pick different names when a collision falls
differently.  Measured on APIC 6.0(9c): **11 properties across 7 of 2,211
generated classes (0.07%)** — e.g. the catalogue names ``l3extOut.enforceRtctrl``
``enforce_route_control`` where the model names it ``enforce_rtctrl``.  This is
**invisible on result objects** — a generated class is served by its typed model,
never the catalogue — and shows only when introspecting those classes with
:meth:`Catalog.describe`.
"""

from __future__ import annotations

import json
import sqlite3
import zlib
from dataclasses import dataclass
from pathlib import Path

from niwaki._codegen._label_utils import resolve_py_names
from niwaki._codegen.basetypes import kind_value_or_none

DEFAULT_PATH = Path(__file__).resolve().parent / "catalog.db"


@dataclass(frozen=True, slots=True)
class ClassMeta:
    """The read metadata for one ACI class, assembled once and memoised.

    Attributes:
        class_name:       The wire class name (e.g. ``"fvCEp"``).
        readable_to_wire: ``{python_name: wire_name}`` — the readable field names.
        wire_to_readable: ``{wire_name: python_name}`` — the inverse.
        wire_to_kind:     ``{wire_name: FieldKind value | None}`` — how to coerce
                          a wire value on read (``None`` = read as a plain string).
        naming:           The wire names of the identifying properties.
    """

    class_name: str
    readable_to_wire: dict[str, str]
    wire_to_readable: dict[str, str]
    wire_to_kind: dict[str, str | None]
    naming: frozenset[str]


@dataclass(frozen=True, slots=True)
class PropDoc:
    """One property, as ``describe`` presents it."""

    readable: str
    wire: str
    kind: str | None
    is_naming: bool
    label: str
    default: object | None
    comment: str
    enum_values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ClassDoc:
    """A class, as ``describe`` presents it: identity, properties, faults, kin."""

    name: str
    label: str
    comment: str
    is_abstract: bool
    props: tuple[PropDoc, ...]
    faults: dict[str, str]
    concrete_subclasses: tuple[str, ...]


class Catalog:
    """A lazy, read-only reader over the catalogue ``.db``.

    Construct with a path (tests inject a fixture ``.db``); the module-level
    :func:`catalog` returns the shipped one.  The connection opens on the first
    query, never at construction, so importing this module is cheap.

    **Concurrency**: the ``.db`` is opened ``immutable=1`` / ``query_only`` and
    shared with ``check_same_thread=False``; the memoisation caches are unlocked
    but every race is idempotent (the same value is recomputed, last write wins),
    so concurrent reads are safe without a lock.
    """

    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        self._path = path
        self._con: sqlite3.Connection | None = None
        self._labels: dict[int, str] | None = None
        self._types: dict[int, str] | None = None
        self._comments: dict[int, str] | None = None
        self._flag_order: list[str] | None = None
        self._fts: bool | None = None
        self._meta: dict[str, ClassMeta] = {}

    @property
    def _connection(self) -> sqlite3.Connection:
        if self._con is None:
            if not self._path.exists():
                raise FileNotFoundError(
                    f"read catalogue not found at {self._path}. It ships with the "
                    "package; in a source checkout run "
                    "'uv run python -m niwaki._codegen.generate_catalog'."
                )
            # immutable=1: read-only, no locking, safe to share across threads.
            self._con = sqlite3.connect(
                f"file:{self._path}?mode=ro&immutable=1", uri=True, check_same_thread=False
            )
            self._con.execute("PRAGMA query_only=1")
        return self._con

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    # ── Pools and manifest (loaded once, into dicts) ──────────────────────────

    def _label(self, pool_id: int | None) -> str:
        if self._labels is None:
            self._labels = dict(self._connection.execute("SELECT id, text FROM label_pool"))
        return "" if pool_id is None else self._labels.get(pool_id, "")

    def _type(self, pool_id: int | None) -> str:
        if self._types is None:
            self._types = dict(self._connection.execute("SELECT id, value FROM type_pool"))
        return "" if pool_id is None else self._types.get(pool_id, "")

    def _naming_bit(self) -> int:
        if self._flag_order is None:
            (raw,) = self._connection.execute(
                "SELECT value FROM manifest WHERE key='prop_flags'"
            ).fetchone()
            self._flag_order = str(raw).split(",")
        return 1 << self._flag_order.index("isNaming")

    # ── Class metadata (the hot path — one query per class, then memoised) ────

    def class_meta(self, class_name: str) -> ClassMeta:
        """Return the memoised :class:`ClassMeta` for a class (builds it once).

        Args:
            class_name: The wire class name, e.g. ``"fvCEp"``.

        Returns:
            The class's read metadata.

        Raises:
            KeyError: No such class in the catalogue.
        """
        got = self._meta.get(class_name)
        if got is None:
            got = self._meta[class_name] = self._build_class_meta(class_name)
        return got

    def _build_class_meta(self, class_name: str) -> ClassMeta:
        con = self._connection
        row = con.execute("SELECT id FROM mo WHERE class_name=?", (class_name,)).fetchone()
        if row is None:
            raise KeyError(class_name)
        class_id = row[0]
        naming_bit = self._naming_bit()

        shaped: dict[str, dict[str, object]] = {}
        wire_to_kind: dict[str, str | None] = {}
        naming: set[str] = set()
        for wire, label_id, base_type_id, flags in con.execute(
            "SELECT wire_name, label_id, base_type_id, flags FROM prop WHERE class_id=?",
            (class_id,),
        ):
            is_naming = bool(flags & naming_bit)
            shaped[wire] = {"label": self._label(label_id), "is_naming": is_naming}
            wire_to_kind[wire] = kind_value_or_none(self._type(base_type_id))
            if is_naming:
                naming.add(wire)

        sm_class = dict(
            con.execute("SELECT wire_name, sm_label FROM scopemeta WHERE class_id=?", (class_id,))
        )
        # Same function, same inputs as the generator → identical field names.
        wire_to_readable = resolve_py_names(shaped, sm_class, class_name)
        readable_to_wire = {py: wire for wire, py in wire_to_readable.items()}
        return ClassMeta(
            class_name=class_name,
            readable_to_wire=readable_to_wire,
            wire_to_readable=wire_to_readable,
            wire_to_kind=wire_to_kind,
            naming=frozenset(naming),
        )

    def _comment_text(self, pool_id: int | None) -> str:
        if pool_id is None:
            return ""
        if self._comments is None:
            self._comments = dict(self._connection.execute("SELECT id, text FROM comment_pool"))
        raw = self._comments.get(pool_id)
        if raw is None:
            return ""
        value = json.loads(raw)
        return " ".join(str(v) for v in value) if isinstance(value, list) else str(value)

    def _enum_members(self, enum_id: int | None) -> tuple[str, ...]:
        if enum_id is None:
            return ()
        row = self._connection.execute("SELECT content FROM enum WHERE id=?", (enum_id,)).fetchone()
        if row is None:
            return ()
        values = json.loads(zlib.decompress(row[0]))
        return tuple(str(v.get("localName", "")) for v in values if isinstance(v, dict))

    # ── Description and discovery (the cold, occasional path) ─────────────────

    def describe(self, class_name: str) -> ClassDoc:
        """Full description of a class: identity, properties, faults, subclasses.

        Args:
            class_name: The wire class name, e.g. ``"fvBD"``.

        Returns:
            A :class:`ClassDoc`.

        Raises:
            KeyError: No such class in the catalogue.
        """
        con = self._connection
        row = con.execute(
            "SELECT id, label_id, comment_id, is_abstract FROM mo WHERE class_name=?",
            (class_name,),
        ).fetchone()
        if row is None:
            raise KeyError(class_name)
        class_id, label_id, comment_id, is_abstract = row
        meta = self.class_meta(class_name)
        props: list[PropDoc] = []
        for wire, p_label, p_comment, enum_id, default_val in con.execute(
            "SELECT wire_name, label_id, comment_id, enum_id, default_val "
            "FROM prop WHERE class_id=? ORDER BY wire_name",
            (class_id,),
        ):
            w = str(wire)
            props.append(
                PropDoc(
                    readable=meta.wire_to_readable.get(w, w),
                    wire=w,
                    kind=meta.wire_to_kind.get(w),
                    is_naming=w in meta.naming,
                    label=self._label(p_label),
                    default=json.loads(default_val) if default_val is not None else None,
                    comment=self._comment_text(p_comment),
                    enum_values=self._enum_members(enum_id),
                )
            )
        faults = {
            str(c): str(n)
            for c, n in con.execute("SELECT code, name FROM fault WHERE class_id=?", (class_id,))
        }
        subs = tuple(self.concrete_subclasses(class_name)) if is_abstract else ()
        return ClassDoc(
            name=class_name,
            label=self._label(label_id),
            comment=self._comment_text(comment_id),
            is_abstract=bool(is_abstract),
            props=tuple(props),
            faults=faults,
            concrete_subclasses=subs,
        )

    def prop_meta(self, class_name: str, name: str) -> PropDoc:
        """Metadata for one property, addressed by its readable or wire name."""
        for prop in self.describe(class_name).props:
            if name in (prop.readable, prop.wire):
                return prop
        raise KeyError(f"{class_name}.{name}")

    def concrete_subclasses(self, class_name: str) -> list[str]:
        """Every concrete descendant of a class, walked transitively (for fan-out)."""
        con = self._connection
        visited: set[str] = set()
        out: list[str] = []
        stack = [class_name]
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            row = con.execute(
                "SELECT id, is_abstract FROM mo WHERE class_name=?", (cur,)
            ).fetchone()
            if row is None:
                continue
            class_id, is_abstract = row
            if cur != class_name and not is_abstract:
                out.append(cur)
            for (child,) in con.execute(
                "SELECT subclass FROM subclass WHERE class_id=?", (class_id,)
            ):
                stack.append(str(child).replace(":", ""))
        return sorted(out)

    def fault_name(self, code: str) -> str | None:
        """The fault rule name for a fault code (e.g. ``"F0467"``), if known."""
        row = self._connection.execute(
            "SELECT name FROM fault WHERE code=? ORDER BY name LIMIT 1", (code,)
        ).fetchone()
        return None if row is None else str(row[0])

    # ── Full-text search (FTS5 where available, LIKE fallback everywhere) ─────

    def _fts_available(self) -> bool:
        if self._fts is None:
            try:
                self._connection.execute("SELECT rowid FROM catalog_fts LIMIT 1").fetchone()
                self._fts = True
            except sqlite3.OperationalError:
                self._fts = False  # this build's sqlite lacks FTS5 → LIKE fallback
        return self._fts

    def search(self, term: str, *, limit: int = 50) -> list[str]:
        """Class names whose wire name or GUI label matches ``term``.

        Uses the FTS5 index (ranked) when the runtime's sqlite has it; otherwise a
        ``LIKE`` substring scan of the same text — unranked, and for a multi-word
        term it wants exact adjacency (narrower than FTS), but correct.  Results are
        ordered so the ``LIMIT`` truncates deterministically.
        """
        con = self._connection
        if self._fts_available():
            try:
                rows = con.execute(
                    "SELECT m.class_name FROM catalog_fts f JOIN mo m ON m.id = f.rowid "
                    "WHERE catalog_fts MATCH ? ORDER BY rank LIMIT ?",
                    (term, limit),
                ).fetchall()
                return [str(r[0]) for r in rows]
            except sqlite3.OperationalError:
                pass  # a malformed FTS query → fall through to LIKE
        rows = con.execute(
            "SELECT m.class_name FROM search_doc s JOIN mo m ON m.id = s.class_id "
            "WHERE s.text LIKE ? ORDER BY m.class_name LIMIT ?",
            (f"%{term}%", limit),
        ).fetchall()
        return [str(r[0]) for r in rows]

    def find_prop(self, term: str, *, limit: int = 50) -> list[tuple[str, str]]:
        """``(class, wire property)`` pairs whose property name or label matches ``term``.

        Answers "which class carries a MAC?" — a scan of the ``prop`` table, the
        complement to class-level :meth:`search`.
        """
        rows = self._connection.execute(
            "SELECT m.class_name, p.wire_name FROM prop p JOIN mo m ON m.id = p.class_id "
            "LEFT JOIN label_pool l ON l.id = p.label_id "
            "WHERE p.wire_name LIKE ? OR l.text LIKE ? "
            "ORDER BY m.class_name, p.wire_name LIMIT ?",
            (f"%{term}%", f"%{term}%", limit),
        ).fetchall()
        return [(str(c), str(w)) for c, w in rows]


_instance: Catalog | None = None


def catalog() -> Catalog:
    """Return the process-wide catalogue reader, opening it lazily on first call."""
    global _instance
    if _instance is None:
        _instance = Catalog()
    return _instance
