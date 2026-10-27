"""The private wheel-weight check: ``scripts/package_weight.py``.

The tree guard (``tests/test_package_weight.py``) weighs the files the wheel
ships, straight from ``src/``.  This script weighs the **built archive**, the
thing users download and the offline wheelhouse carries.  The private full
gate builds the wheel and runs it (``scripts/checks.sh``, inside
``run_wheel_smoke``).

The CLI contract these tests pin
--------------------------------
``python scripts/package_weight.py WHEEL [--max-wheel-bytes N] [--max-unpacked-bytes N]``

- Two measures: the **wheel size** (the archive's size on disk, its
  ``st_size``) and the **unpacked size** (the sum of ``file_size`` over every
  member of the archive, dist-info included).  Each is checked against a
  budget, inclusive (a measure equal to its budget passes).
- The budgets default to module-level constants ``MAX_WHEEL_BYTES`` and
  ``MAX_UNPACKED_BYTES``; the two options override them.  A budget must be a
  positive integer, otherwise it is a usage error.
- Exit **0**: both measures within budget.  One line on stdout starting with
  ``[package-weight] OK`` and holding both measures as plain integers (no
  thousands separator), e.g.
  ``[package-weight] OK wheel size 18854774 <= 19000000, unpacked size 75733265 <= 76000000``.
- Exit **1**: at least one measure over budget.  On stderr, one line per
  exceeded measure, each starting with ``[package-weight] FAIL`` and holding
  the measure's name (``wheel size`` or ``unpacked size``), the measured value
  and the budget as plain integers.  A measure within budget gets no FAIL line.
- Exit **2**: the wheel cannot be measured, or the command line is wrong.
  A missing path prints a ``[package-weight]`` line on stderr containing
  ``not found`` and the path; a file that is not a zip archive prints one
  containing ``not a valid wheel``.  No argument, or a non-positive budget,
  is an ``argparse`` usage error (exit 2).
- The script uses the standard library only: it runs under ``python -S``,
  with no site-packages (so with no niwaki installed), as the gate may call it
  from any interpreter before or after the install.

The script is private tooling, like the gate that calls it: these tests run
where the gate scripts exist and skip in the public export.
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).parents[2] / "scripts"
_SCRIPT = _SCRIPTS / "package_weight.py"

if not (_SCRIPTS / "checks.sh").is_file():
    pytest.skip("private gate scripts not present in this distribution", allow_module_level=True)


def _run(*args: str | Path) -> subprocess.CompletedProcess[str]:
    """Run the script in an isolated interpreter with no site-packages.

    Fails first when the script is absent: the interpreter would exit 2 on
    its own, which would pass the usage-error tests for the wrong reason.
    """
    assert _SCRIPT.is_file(), "scripts/package_weight.py is missing"
    return subprocess.run(
        [sys.executable, "-I", "-S", str(_SCRIPT), *(str(arg) for arg in args)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _wheel(path: Path, members: dict[str, bytes]) -> Path:
    """Write a deflate-compressed wheel-shaped archive holding ``members``."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def _sizes(wheel: Path) -> tuple[int, int]:
    """The two measures of ``wheel``, computed independently of the script."""
    with zipfile.ZipFile(wheel) as archive:
        unpacked = sum(info.file_size for info in archive.infolist())
    return wheel.stat().st_size, unpacked


@pytest.fixture
def wheel(tmp_path: Path) -> Path:
    """A small synthetic wheel: incompressible-ish code plus a compressible blob."""
    return _wheel(
        tmp_path / "pkg-1.0-py3-none-any.whl",
        {
            "pkg/__init__.py": b'"""A package."""\n' * 40,
            "pkg/data.db": b"\x00" * 50_000,  # deflates to almost nothing
            "pkg-1.0.dist-info/METADATA": b"Metadata-Version: 2.4\nName: pkg\nVersion: 1.0\n",
            "pkg-1.0.dist-info/RECORD": b"",
        },
    )


def test_the_script_exists() -> None:
    assert _SCRIPT.is_file(), "scripts/package_weight.py is missing"


# ── Nominal ───────────────────────────────────────────────────────────────────


def test_within_default_budgets_passes(wheel: Path) -> None:
    """The built-in budgets admit a small wheel; the report names both measures."""
    size, unpacked = _sizes(wheel)
    result = _run(wheel)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("[package-weight] OK")
    assert str(size) in result.stdout
    assert str(unpacked) in result.stdout
    assert "FAIL" not in result.stdout + result.stderr


def test_within_explicit_budgets_passes(wheel: Path) -> None:
    size, unpacked = _sizes(wheel)
    result = _run(
        wheel, "--max-wheel-bytes", str(size + 1), "--max-unpacked-bytes", str(unpacked + 1)
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("[package-weight] OK")


def test_budget_equal_to_measure_passes(wheel: Path) -> None:
    """The budgets are inclusive."""
    size, unpacked = _sizes(wheel)
    result = _run(wheel, "--max-wheel-bytes", str(size), "--max-unpacked-bytes", str(unpacked))
    assert result.returncode == 0, result.stderr


def test_unpacked_size_counts_every_member(wheel: Path) -> None:
    """dist-info members count: the budget sits one byte under the full sum."""
    size, unpacked = _sizes(wheel)
    result = _run(wheel, "--max-wheel-bytes", str(size), "--max-unpacked-bytes", str(unpacked - 1))
    assert result.returncode == 1


def test_empty_wheel_passes(tmp_path: Path) -> None:
    """An archive with no member has an unpacked size of 0 and still measures."""
    empty = _wheel(tmp_path / "empty-1.0-py3-none-any.whl", {})
    result = _run(empty)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("[package-weight] OK")


# ── Over budget ───────────────────────────────────────────────────────────────


def test_wheel_over_budget_fails(wheel: Path) -> None:
    size, unpacked = _sizes(wheel)
    result = _run(wheel, "--max-wheel-bytes", str(size - 1), "--max-unpacked-bytes", str(unpacked))
    assert result.returncode == 1
    fails = [
        line for line in result.stderr.splitlines() if line.startswith("[package-weight] FAIL")
    ]
    assert len(fails) == 1, result.stderr
    assert "wheel size" in fails[0]
    assert str(size) in fails[0] and str(size - 1) in fails[0]
    assert not result.stdout.startswith("[package-weight] OK")


def test_unpacked_over_budget_fails_even_when_the_archive_is_small(wheel: Path) -> None:
    """Deflate hides a blob in the archive; the unpacked budget still sees it."""
    size, unpacked = _sizes(wheel)
    assert size < unpacked  # the synthetic blob compresses
    result = _run(wheel, "--max-wheel-bytes", str(size), "--max-unpacked-bytes", "1000")
    assert result.returncode == 1
    fails = [
        line for line in result.stderr.splitlines() if line.startswith("[package-weight] FAIL")
    ]
    assert len(fails) == 1, result.stderr
    assert "unpacked size" in fails[0]
    assert str(unpacked) in fails[0] and "1000" in fails[0]


def test_both_over_budget_report_both(wheel: Path) -> None:
    result = _run(wheel, "--max-wheel-bytes", "1", "--max-unpacked-bytes", "1")
    assert result.returncode == 1
    fails = [
        line for line in result.stderr.splitlines() if line.startswith("[package-weight] FAIL")
    ]
    assert len(fails) == 2, result.stderr
    assert any("wheel size" in line for line in fails)
    assert any("unpacked size" in line for line in fails)


# ── Cannot measure / usage errors ─────────────────────────────────────────────


def test_missing_wheel_exits_2(tmp_path: Path) -> None:
    absent = tmp_path / "niwaki-9.9.9-py3-none-any.whl"
    result = _run(absent)
    assert result.returncode == 2
    assert "[package-weight]" in result.stderr
    assert "not found" in result.stderr
    assert str(absent) in result.stderr
    assert "Traceback" not in result.stderr


def test_not_a_zip_exits_2(tmp_path: Path) -> None:
    bogus = tmp_path / "bogus-1.0-py3-none-any.whl"
    bogus.write_bytes(b"this is not a zip archive")
    result = _run(bogus)
    assert result.returncode == 2
    assert "not a valid wheel" in result.stderr
    assert "Traceback" not in result.stderr


def test_no_argument_is_a_usage_error() -> None:
    result = _run()
    assert result.returncode == 2
    assert "usage" in result.stderr.lower()


@pytest.mark.parametrize("budget", ["0", "-5", "lots"])
@pytest.mark.parametrize("option", ["--max-wheel-bytes", "--max-unpacked-bytes"])
def test_invalid_budget_is_a_usage_error(wheel: Path, option: str, budget: str) -> None:
    result = _run(wheel, option, budget)
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
