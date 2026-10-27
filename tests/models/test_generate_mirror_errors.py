"""Error and pruning paths of ``niwaki._codegen.generate_mirror``.

The nominal contract (render/write on the real and a synthetic tree, the
manifest and regen wiring) is pinned in ``test_import_mirror_fresh.py``.
This module covers what the generator must *refuse* — canonical layouts it
cannot re-export faithfully — and the directory hygiene of ``write_mirror``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from niwaki._codegen.generate_mirror import MirrorSourceError, render_mirror, write_mirror

_PKG_INIT = (
    '_CLASSES: dict[str, str] = {"Foo": "xyFoo"}\n\n\n'
    "def __getattr__(name: str) -> type:\n"
    "    raise AttributeError(name)\n\n\n"
    "__all__ = list(_CLASSES)\n"
)


def _tree(root: Path, files: dict[str, str]) -> Path:
    gen = root / "_generated"
    gen.mkdir()
    (gen / "__init__.py").write_text("", encoding="utf-8")
    for rel, text in files.items():
        path = gen / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return gen


# ── Refusals: never guess a re-export ─────────────────────────────────────────


def test_a_package_with_code_but_no_short_name_table_is_refused(tmp_path: Path) -> None:
    gen = _tree(tmp_path, {"xy/__init__.py": "VALUE = 1\n"})
    with pytest.raises(MirrorSourceError, match=r"no _CLASSES"):
        render_mirror(gen)


def test_a_package_table_without_all_is_refused(tmp_path: Path) -> None:
    gen = _tree(tmp_path, {"xy/__init__.py": '_CLASSES: dict[str, str] = {"Foo": "xyFoo"}\n'})
    with pytest.raises(MirrorSourceError, match=r"no __all__"):
        render_mirror(gen)


def test_a_non_literal_short_name_table_is_refused(tmp_path: Path) -> None:
    gen = _tree(tmp_path, {"xy/__init__.py": "_CLASSES = {1: 2}\n__all__ = []\n"})
    with pytest.raises(MirrorSourceError, match=r"literal dict"):
        render_mirror(gen)


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("from .XyMode import XyMode\n", "absolute from-imports"),
        ("import niwaki\n", "absolute from-imports"),
        ("X = 1\n", "absolute from-imports"),
        ("from niwaki.models._generated.enums.XyMode import *\n", "star imports"),
        ('"""Only a docstring."""\nfrom __future__ import annotations\n', "re-exports nothing"),
    ],
    ids=["relative", "plain-import", "assignment", "star", "empty"],
)
def test_an_unexportable_barrel_is_refused(tmp_path: Path, body: str, match: str) -> None:
    gen = _tree(tmp_path, {"enums/__init__.py": "", "enums/_all.py": body})
    with pytest.raises(MirrorSourceError, match=match):
        render_mirror(gen)


def test_a_root_that_is_a_file_is_not_a_tree(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "_generated"
    not_a_dir.write_text("", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        render_mirror(not_a_dir)


# ── Shapes at the edge ────────────────────────────────────────────────────────


def test_a_barrel_keeps_the_canonical_bound_name(tmp_path: Path) -> None:
    gen = _tree(
        tmp_path,
        {"enums/_all.py": "from niwaki.models._generated.enums.XyMode import XyMode as Mode\n"},
    )
    text = render_mirror(gen)[Path("enums") / "_all.py"]
    assert "from niwaki.models._generated.enums.XyMode import XyMode as Mode\n" in text


def test_an_empty_package_mirrors_without_all(tmp_path: Path) -> None:
    gen = _tree(tmp_path, {"enums/__init__.py": '"""Docstring only."""\n'})
    text = render_mirror(gen)[Path("enums") / "__init__.py"]
    assert "__all__" not in text
    assert "__getattr__" not in text


def test_an_empty_tree_renders_nothing(tmp_path: Path) -> None:
    assert render_mirror(_tree(tmp_path, {})) == {}


def test_rendered_package_preserves_the_canonical_table_order(tmp_path: Path) -> None:
    table = (
        '_CLASSES: dict[str, str] = {"Zed": "xyZed", "Alpha": "xyAlpha"}\n'
        "__all__ = list(_CLASSES)\n"
    )
    gen = _tree(tmp_path, {"xy/__init__.py": table})
    text = render_mirror(gen)[Path("xy") / "__init__.py"]
    assert text.index('"Zed"') < text.index('"Alpha"')


# ── write_mirror directory hygiene ────────────────────────────────────────────


def test_write_removes_emptied_mirror_directories_and_their_caches(tmp_path: Path) -> None:
    gen = _tree(tmp_path, {"xy/__init__.py": _PKG_INIT, "xy/xyFoo.py": "class xyFoo: ...\n"})
    stale = tmp_path / "gone"
    (stale / "__pycache__").mkdir(parents=True)
    (stale / "__pycache__" / "gone.cpython-312.pyc").write_bytes(b"\x00")
    (stale / "__init__.py").write_text("# stale\n", encoding="utf-8")

    write_mirror(tmp_path, gen)

    assert not stale.exists()
    assert (tmp_path / "xy" / "xyFoo.py").is_file()


def test_write_never_deletes_a_non_module_file(tmp_path: Path) -> None:
    """A directory still holding a foreign (non-``.py``) file is left in place."""
    gen = _tree(tmp_path, {"xy/__init__.py": _PKG_INIT})
    stray = tmp_path / "gone" / "README.txt"
    stray.parent.mkdir()
    stray.write_text("kept\n", encoding="utf-8")
    (tmp_path / "gone" / "old.py").write_text("# stale\n", encoding="utf-8")

    write_mirror(tmp_path, gen)

    assert stray.read_text(encoding="utf-8") == "kept\n"
    assert not (tmp_path / "gone" / "old.py").exists()


def test_write_leaves_an_unchanged_file_untouched(tmp_path: Path) -> None:
    gen = _tree(tmp_path, {"xy/__init__.py": _PKG_INIT, "xy/xyFoo.py": "class xyFoo: ...\n"})
    write_mirror(tmp_path, gen)
    leaf = tmp_path / "xy" / "xyFoo.py"
    before = leaf.stat().st_mtime_ns
    write_mirror(tmp_path, gen)
    assert leaf.stat().st_mtime_ns == before


def test_write_with_a_missing_generated_tree_fails_before_writing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        write_mirror(tmp_path)
    assert list(tmp_path.iterdir()) == []
