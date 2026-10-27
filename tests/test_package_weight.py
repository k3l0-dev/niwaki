"""The installed-weight ratchet: what the wheel ships may only shrink.

niwaki installs about 75 MB of sources and data (twice that once pip has
compiled the bytecode).  Nothing used to watch that number, so it could grow
by megabytes in a regen without anyone seeing it.  These tests pin it.

How the ratchet works
---------------------
Every measure has a budget.  A measure **above** its budget fails: the
change made the package heavier, and someone has to decide that on purpose
by raising the budget in the same commit.  A measure **far below** its budget
fails too (``test_budgets_are_tight``, under ``RATCHET`` of the budget): a
change that saved weight must lower the budget in the same commit, so the
saving cannot be eaten back silently later.  The failure message gives the
value to write.

The measures come from the source tree (``tests/_package_weight.py`` explains
how, and why it matches the wheel byte for byte), so these tests need no
build and no schema corpus: they run on the public CI.  The size of the
built archive itself is checked by the private gate
(``scripts/package_weight.py``, called by ``scripts/checks.sh``).

Budgets are in bytes, decimal megabytes (``1 MB = 1_000_000``).
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from tests._package_weight import (
    PROJECT_ROOT,
    WheelLayout,
    read_wheel_layout,
    shipped_files,
    shipped_weight,
)

# ── Budgets (lower them as the package loses weight) ──────────────────────────

# Raised for the statically-resolvable model import mirror (3,074 re-export modules)
# and the credential helpers (transport/_credentials.py).
# Raised for the public raw-JSON aliases (niwaki/types.py, niwaki/_json.py helpers).
# Lowered by the shared fault-code register (one copy, not ten) and the read
# catalogue's shared property signatures (prop_sig + class_prop); raised for the
# typed navigation stubs (134 .pyi files).
BUDGET_TREE: int = 43_070_000
"""Every file the wheel ships (measured 43,061,762 bytes on 2026-10-27)."""

BUDGET_DB: int = 26_000_000
"""The read catalogue, ``query/_catalog/catalog.db`` (25,993,216 bytes)."""

BUDGET_MODELS: int = 10_070_000
"""The generated models, ``models/_generated`` (10,061,309 bytes)."""

CEILING: int = 1_000_000
"""No single shipped file may weigh more than this, unless listed below."""

CEILING_EXCEPTIONS: dict[str, int] = {
    # The read catalogue: one file by design, governed by its own budget.
    "query/_catalog/catalog.db": BUDGET_DB,
    # The tenant-domain cursors: verbose docstrings by rule (IDE help on every
    # maker), and the tenant is the largest curated domain.
    "design/_generated_cursors/_tenant.py": 1_300_000,
    # The shared fault-code register, one copy instead of five: the 80,512
    # fault codes of faultInst, faultLcP, faultSevAsnP, eventSevAsnP and
    # callhomeSmartCallhomeFault, encoded as text.  Pinned at its exact size.
    "models/_generated/_registers/__init__.py": 1_222_511,
}
"""Shipped files allowed over :data:`CEILING`, each with its own size cap.

Keys are paths relative to the package directory (``src/niwaki``), in POSIX
form.  An exception that no longer exists, or no longer exceeds the ceiling,
is stale and fails the guard: remove it.
"""

RATCHET: float = 0.95
"""A measure under this fraction of its budget means the budget is too loose."""


# ── Helpers ───────────────────────────────────────────────────────────────────


def _layout() -> WheelLayout:
    return read_wheel_layout()


def _package_files() -> dict[str, int]:
    """Every shipped file, keyed by its POSIX path relative to the package."""
    layout = _layout()
    return {
        path.relative_to(layout.package).as_posix(): path.stat().st_size
        for path in shipped_files(layout)
    }


def _weight_under(prefix: str) -> int:
    """The summed size of the shipped files under ``prefix`` (a directory)."""
    return sum(size for name, size in _package_files().items() if name.startswith(prefix + "/"))


def _suggest(measure: int) -> int:
    """A budget just above ``measure``: rounded up to the next 10,000 bytes."""
    return max(10_000, math.ceil((measure + 1) / 10_000) * 10_000)


def _measures() -> list[tuple[str, int, int]]:
    """Every ``(name, measure, budget)`` pair the ratchet watches."""
    files = _package_files()
    pairs = [
        ("BUDGET_TREE", sum(files.values()), BUDGET_TREE),
        ("BUDGET_DB", files["query/_catalog/catalog.db"], BUDGET_DB),
        ("BUDGET_MODELS", _weight_under("models/_generated"), BUDGET_MODELS),
    ]
    pairs.extend(
        (f"CEILING_EXCEPTIONS[{name!r}]", files[name], cap)
        for name, cap in CEILING_EXCEPTIONS.items()
        if name in files
    )
    return pairs


# ── The budgets ───────────────────────────────────────────────────────────────


def test_shipped_tree_under_budget() -> None:
    """Everything the wheel ships, uncompressed, stays within its budget."""
    measure = shipped_weight(_layout())
    assert measure <= BUDGET_TREE, (
        f"the shipped tree weighs {measure:,} bytes, over BUDGET_TREE = {BUDGET_TREE:,}. "
        f"If the growth is intended, raise BUDGET_TREE to {_suggest(measure)} in this commit."
    )


def test_catalog_db_under_budget() -> None:
    """The read catalogue ships, and stays within its own budget."""
    files = _package_files()
    # The v1.2.0 near-miss: the catalogue was built but never shipped.
    assert "query/_catalog/catalog.db" in files, "catalog.db is no longer part of the wheel"
    measure = files["query/_catalog/catalog.db"]
    assert measure <= BUDGET_DB, (
        f"catalog.db weighs {measure:,} bytes, over BUDGET_DB = {BUDGET_DB:,}. "
        f"If the growth is intended, raise BUDGET_DB to {_suggest(measure)} in this commit."
    )


def test_generated_models_under_budget() -> None:
    """The generated models (``models/_generated``) stay within their budget."""
    measure = _weight_under("models/_generated")
    assert measure > 0, "no generated model found under models/_generated"
    assert measure <= BUDGET_MODELS, (
        f"models/_generated weighs {measure:,} bytes, over BUDGET_MODELS = {BUDGET_MODELS:,}. "
        f"If the growth is intended, raise BUDGET_MODELS to {_suggest(measure)} in this commit."
    )


def test_no_shipped_file_over_ceiling() -> None:
    """No shipped file exceeds the ceiling, except the listed ones within their cap."""
    files = _package_files()
    unlisted = {
        name: size
        for name, size in files.items()
        if size > CEILING and name not in CEILING_EXCEPTIONS
    }
    assert not unlisted, (
        f"shipped files over the {CEILING:,}-byte ceiling: {unlisted}. "
        "Make them smaller, or list them in CEILING_EXCEPTIONS with a reason."
    )
    over_cap = {
        name: (files[name], cap)
        for name, cap in CEILING_EXCEPTIONS.items()
        if name in files and files[name] > cap
    }
    assert not over_cap, f"listed files over their own cap (size, cap): {over_cap}"
    missing = sorted(name for name in CEILING_EXCEPTIONS if name not in files)
    assert not missing, f"stale CEILING_EXCEPTIONS (file no longer shipped), remove: {missing}"
    under = sorted(name for name in CEILING_EXCEPTIONS if name in files and files[name] <= CEILING)
    assert not under, f"stale CEILING_EXCEPTIONS (now under the ceiling), remove: {under}"


def test_budgets_are_tight() -> None:
    """Each measure sits within ``RATCHET`` of its budget, so savings are locked in."""
    loose = [
        f"{name}: measured {measure:,} is under {RATCHET:.0%} of {budget:,}; "
        f"lower it to {_suggest(measure)}"
        for name, measure, budget in _measures()
        if measure < RATCHET * budget
    ]
    assert not loose, "budgets too loose — the saving must be locked in:\n" + "\n".join(loose)


def test_budget_constants_are_coherent() -> None:
    """The budgets nest: the db and the models are parts of the tree."""
    assert 0 < BUDGET_DB < BUDGET_TREE
    assert 0 < BUDGET_MODELS < BUDGET_TREE
    assert 0 < RATCHET < 1
    assert all(cap > CEILING for cap in CEILING_EXCEPTIONS.values()), (
        "an exception capped at or under the ceiling is not an exception"
    )


# ── The measure itself ────────────────────────────────────────────────────────


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _pyproject(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(body, encoding="utf-8")
    return path


_WHEEL_TABLE = """\
[tool.hatch.build.targets.wheel]
packages = ["src/pkg"]
exclude = ["src/pkg/_codegen"]
"""


class TestWeightMeasureExcludesCodegen:
    """``test_weight_measure_excludes_codegen``: what the measure counts, and refuses."""

    def test_nominal_counts_only_shipped_files(self, tmp_path: Path) -> None:
        package = tmp_path / "src" / "pkg"
        _write(package / "__init__.py", 10)
        _write(package / "sub" / "data.db", 20)
        _write(package / "_codegen" / "generate.py", 1_000)  # excluded by pyproject
        _write(package / "__pycache__" / "__init__.cpython-313.pyc", 500)
        _write(package / "sub" / "__pycache__" / "x.cpython-313.pyc", 500)
        _write(package / "sub" / "stray.pyc", 7)
        _write(package / "sub" / "stray.pyo", 7)
        _write(package / ".DS_Store", 6_000)
        layout = read_wheel_layout(_pyproject(tmp_path, _WHEEL_TABLE))

        assert layout.package == package.resolve()
        assert layout.excludes == ((package / "_codegen").resolve(),)
        assert [path.relative_to(layout.package).as_posix() for path in shipped_files(layout)] == [
            "__init__.py",
            "sub/data.db",
        ]
        assert shipped_weight(layout) == 30

    def test_exclusion_is_a_path_prefix_not_a_name_prefix(self, tmp_path: Path) -> None:
        """``_codegen`` excluded does not drop a sibling ``_codegen_extra``."""
        package = tmp_path / "src" / "pkg"
        _write(package / "_codegen" / "a.py", 100)
        _write(package / "_codegen_extra.py", 3)
        layout = read_wheel_layout(_pyproject(tmp_path, _WHEEL_TABLE))
        assert shipped_weight(layout) == 3

    def test_a_single_file_can_be_excluded(self, tmp_path: Path) -> None:
        package = tmp_path / "src" / "pkg"
        _write(package / "keep.py", 4)
        _write(package / "drop.py", 40)
        body = (
            "[tool.hatch.build.targets.wheel]\n"
            'packages = ["src/pkg"]\n'
            'exclude = ["src/pkg/drop.py"]\n'
        )
        layout = read_wheel_layout(_pyproject(tmp_path, body))
        assert shipped_weight(layout) == 4

    def test_no_exclude_key_ships_everything(self, tmp_path: Path) -> None:
        package = tmp_path / "src" / "pkg"
        _write(package / "_codegen" / "a.py", 5)
        body = '[tool.hatch.build.targets.wheel]\npackages = ["src/pkg"]\n'
        layout = read_wheel_layout(_pyproject(tmp_path, body))
        assert layout.excludes == ()
        assert shipped_weight(layout) == 5

    def test_sizes_are_apparent_not_allocated(self, tmp_path: Path) -> None:
        """A sparse file weighs its ``st_size``, whatever the disk holds."""
        package = tmp_path / "pkg"
        package.mkdir()
        with (package / "sparse.bin").open("wb") as handle:
            handle.truncate(3_000_000)
        assert shipped_weight(WheelLayout(package=package)) == 3_000_000

    def test_empty_tree_weighs_nothing(self, tmp_path: Path) -> None:
        package = tmp_path / "pkg"
        package.mkdir()
        layout = WheelLayout(package=package)
        assert shipped_files(layout) == []
        assert shipped_weight(layout) == 0

    def test_tree_of_only_bytecode_weighs_nothing(self, tmp_path: Path) -> None:
        package = tmp_path / "pkg"
        _write(package / "__pycache__" / "m.cpython-313.pyc", 50)
        assert shipped_weight(WheelLayout(package=package)) == 0

    def test_missing_package_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="package directory not found"):
            shipped_weight(WheelLayout(package=tmp_path / "absent"))

    def test_package_that_is_a_file_raises(self, tmp_path: Path) -> None:
        package = tmp_path / "pkg"
        package.write_bytes(b"not a directory")
        with pytest.raises(NotADirectoryError):
            shipped_weight(WheelLayout(package=package))

    def test_stale_exclusion_raises(self, tmp_path: Path) -> None:
        package = tmp_path / "src" / "pkg"
        _write(package / "a.py", 1)
        layout = read_wheel_layout(_pyproject(tmp_path, _WHEEL_TABLE))  # no _codegen on disk
        with pytest.raises(ValueError, match="matches nothing"):
            shipped_weight(layout)

    def test_missing_pyproject_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            read_wheel_layout(tmp_path / "pyproject.toml")

    def test_missing_wheel_table_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match=r"tool\.hatch\.build\.targets\.wheel"):
            read_wheel_layout(_pyproject(tmp_path, '[project]\nname = "pkg"\n'))

    @pytest.mark.parametrize("packages", ["[]", '["src/a", "src/b"]'])
    def test_not_exactly_one_package_raises(self, tmp_path: Path, packages: str) -> None:
        body = f"[tool.hatch.build.targets.wheel]\npackages = {packages}\n"
        with pytest.raises(ValueError, match="exactly one wheel package"):
            read_wheel_layout(_pyproject(tmp_path, body))

    @pytest.mark.parametrize(
        "pattern", ["src/pkg/*.db", "**/_codegen", "src/pkg/[ab].py", "!src/pkg/x"]
    )
    def test_pattern_exclusion_raises(self, tmp_path: Path, pattern: str) -> None:
        body = (
            f'[tool.hatch.build.targets.wheel]\npackages = ["src/pkg"]\nexclude = ["{pattern}"]\n'
        )
        with pytest.raises(ValueError, match="is a pattern"):
            read_wheel_layout(_pyproject(tmp_path, body))

    def test_the_real_layout_leaves_codegen_out(self) -> None:
        """The repository's own pyproject excludes ``_codegen``, and the measure honours it."""
        layout = _layout()
        codegen = PROJECT_ROOT / "src" / "niwaki" / "_codegen"
        assert layout.package == PROJECT_ROOT / "src" / "niwaki"
        assert codegen in layout.excludes
        assert codegen.is_dir(), "the generators moved: update this guard"
        files = shipped_files(layout)
        assert files, "the measure found no shipped file at all"
        assert not [path for path in files if path.is_relative_to(codegen)]
        assert not [path for path in files if path.suffix == ".pyc" or "__pycache__" in path.parts]
