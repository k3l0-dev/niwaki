"""The shipped catalogue stores properties as shared signatures plus memberships.

332,297 ``(class, property)`` rows carry only 25,901 distinct signatures:
``status``/``rn``/``dn``/``childAction`` alone repeat on all 15,452 classes.
The normalised layout keeps each signature once and links classes to them:

- ``prop_sig`` — one row per distinct property signature (every column of the
  old ``prop`` table except ``class_id``), keyed by an integer id;
- ``class_prop`` — ``(class_id, sig_id)``, two integers, ``WITHOUT ROWID``,
  primary key ``(class_id, sig_id)``: a class's memberships sit together in
  the b-tree, which is what a per-class lookup reads.

There is no ``prop`` table any more, and no ``prop`` view either: the reader's
queries are rewritten on ``class_prop JOIN prop_sig`` (a view kept the reader
unchanged but made ``find_prop`` about 40x slower).

Read straight from the committed db (corpus-free, so the public CI runs it).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from niwaki.query import _catalog

#: Distinct property signatures in the 6.0(9c) corpus — every column of the
#: old ``prop`` table except ``class_id``, ``residual`` included (NULL equal
#: to NULL, as ``SELECT DISTINCT`` counts them).  Measured on the shipped db.
SIGNATURES = 25901
#: ``(class, property)`` memberships — the old ``prop`` row count.
MEMBERSHIPS = 332297


@pytest.fixture(scope="module")
def con() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(f"file:{_catalog.DEFAULT_PATH}?mode=ro", uri=True)
    yield connection
    connection.close()


def _object_type(con: sqlite3.Connection, name: str) -> str | None:
    row = con.execute("SELECT type FROM sqlite_master WHERE name=?", (name,)).fetchone()
    return None if row is None else str(row[0])


def _columns(con: sqlite3.Connection, table: str) -> list[tuple[str, str, int]]:
    """``(name, declared type, pk position)`` per column, in declaration order."""
    # ``table`` is a literal from this module, never user input.
    return [
        (str(name), str(decl).upper(), int(pk))
        for _cid, name, decl, _notnull, _default, pk in con.execute(f"PRAGMA table_info({table})")
    ]


def test_signature_and_membership_tables_exist(con: sqlite3.Connection) -> None:
    assert _object_type(con, "prop_sig") == "table"
    assert _object_type(con, "class_prop") == "table"


def test_class_prop_is_two_integers_keyed_by_class_then_signature(
    con: sqlite3.Connection,
) -> None:
    columns = _columns(con, "class_prop")
    assert [name for name, _decl, _pk in columns] == ["class_id", "sig_id"]
    assert all(decl.startswith("INT") for _name, decl, _pk in columns), columns
    assert {name: pk for name, _decl, pk in columns} == {"class_id": 1, "sig_id": 2}


def test_class_prop_is_without_rowid(con: sqlite3.Connection) -> None:
    (sql,) = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='class_prop'"
    ).fetchone()
    assert "WITHOUT ROWID" in str(sql).upper()
    # The engine agrees, not just the DDL text: a rowid table answers this.
    with pytest.raises(sqlite3.OperationalError, match="rowid"):
        con.execute("SELECT rowid FROM class_prop LIMIT 1").fetchone()


def test_no_prop_table_remains(con: sqlite3.Connection) -> None:
    assert _object_type(con, "prop") != "table"


def test_no_prop_view_stands_in_for_it(con: sqlite3.Connection) -> None:
    """The reader is rewritten on the join — no compatibility view."""
    assert _object_type(con, "prop") is None


def test_every_signature_is_distinct(con: sqlite3.Connection) -> None:
    """Normalised means no two ``prop_sig`` rows differ only by their id."""
    payload = [name for name, _decl, pk in _columns(con, "prop_sig") if pk == 0]
    assert "wire_name" in payload
    assert "residual" in payload  # a signature differing only by residual stays apart
    (total,) = con.execute("SELECT COUNT(*) FROM prop_sig").fetchone()
    (distinct,) = con.execute(
        f"SELECT COUNT(*) FROM (SELECT DISTINCT {', '.join(payload)} FROM prop_sig)"
    ).fetchone()
    assert total == distinct == SIGNATURES


def test_memberships_cover_every_property(con: sqlite3.Connection) -> None:
    (total,) = con.execute("SELECT COUNT(*) FROM class_prop").fetchone()
    assert total == MEMBERSHIPS


def test_memberships_reference_real_rows(con: sqlite3.Connection) -> None:
    sig_pk = [name for name, _decl, pk in _columns(con, "prop_sig") if pk == 1]
    assert len(sig_pk) == 1, f"prop_sig needs a single-column key, got {sig_pk}"
    (orphan_sigs,) = con.execute(
        f"SELECT COUNT(*) FROM class_prop c LEFT JOIN prop_sig s ON s.{sig_pk[0]} = c.sig_id "
        f"WHERE s.{sig_pk[0]} IS NULL"
    ).fetchone()
    (orphan_classes,) = con.execute(
        "SELECT COUNT(*) FROM class_prop c LEFT JOIN mo m ON m.id = c.class_id WHERE m.id IS NULL"
    ).fetchone()
    (unused_sigs,) = con.execute(
        # NOT IN, not a correlated NOT EXISTS: class_prop is keyed by class
        # first, and the subquery gets an ephemeral index on sig_id.
        f"SELECT COUNT(*) FROM prop_sig WHERE {sig_pk[0]} NOT IN (SELECT sig_id FROM class_prop)"
    ).fetchone()
    assert (orphan_sigs, orphan_classes, unused_sigs) == (0, 0, 0)


def test_a_class_never_holds_one_wire_name_twice(con: sqlite3.Connection) -> None:
    """The old primary key ``(class_id, wire_name)``, kept as an invariant."""
    keys = [name for name, _decl, pk in _columns(con, "prop_sig") if pk == 1]
    assert len(keys) == 1, f"prop_sig needs a single-column key, got {keys}"
    sig_pk = keys[0]
    (dupes,) = con.execute(
        "SELECT COUNT(*) FROM (SELECT c.class_id, s.wire_name FROM class_prop c "
        f"JOIN prop_sig s ON s.{sig_pk} = c.sig_id "
        "GROUP BY c.class_id, s.wire_name HAVING COUNT(*) > 1)"
    ).fetchone()
    assert dupes == 0
