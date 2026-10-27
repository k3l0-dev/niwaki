"""Measure what the niwaki wheel installs, straight from the source tree.

The weight guard (``tests/test_package_weight.py``) needs the size of the
files the wheel ships, without building the wheel: building takes seconds and
is the private gate's job (``scripts/package_weight.py``, called by
``scripts/checks.sh``).  This module computes that size from ``src/`` alone,
so the guard runs on any checkout, including the public CI.

Why this module lives in ``tests/``
-----------------------------------
- **Not in** ``src/niwaki``: it would ship in the wheel it measures and add a
  dev-only module to the installed package.
- **Not in** ``src/niwaki/_codegen``: that tree is excluded from the wheel,
  but every file in it is a generator input fingerprinted by the freshness
  manifest (``_codegen/freshness.py``), so a measuring helper there would
  force a regen of the manifest for a change that generates nothing.
- **Not in** ``scripts/``: part of that directory is private tooling, and it
  is not an importable package.  The private wheel check measures something
  else anyway (the built archive), so it does not need this module.
- ``tests/`` ships with the public export and is an importable package
  (``from tests._package_weight import ...``), so the public CI imports this
  module the same way the private suite does.  The leading underscore keeps
  pytest from collecting it as a test module.

What counts as shipped
----------------------
The wheel layout is read from ``pyproject.toml``
(``[tool.hatch.build.targets.wheel]``), never restated here: ``packages``
names the package root and ``exclude`` lists the paths the wheel leaves out
(today ``src/niwaki/_codegen``).  On top of those, the measure skips what
the build never picks up from a working tree: ``__pycache__`` directories,
compiled bytecode (``*.pyc``, ``*.pyo``) and Finder litter (``.DS_Store``),
which are all ignored by the project's ``.gitignore``.

Sizes are **apparent** sizes (``st_size``), not disk usage: they do not depend
on the filesystem block size or on sparse allocation, so the same tree weighs
the same on APFS, ext4 and NTFS.  They match the uncompressed size of the
package files inside the wheel byte for byte.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "PROJECT_ROOT",
    "PYPROJECT",
    "WheelLayout",
    "read_wheel_layout",
    "shipped_files",
    "shipped_weight",
]

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
"""The repository root (the directory that holds ``pyproject.toml``)."""

PYPROJECT: Path = PROJECT_ROOT / "pyproject.toml"
"""The project file the wheel layout is read from."""

_SKIPPED_DIRS: frozenset[str] = frozenset({"__pycache__"})
_SKIPPED_SUFFIXES: frozenset[str] = frozenset({".pyc", ".pyo"})
_SKIPPED_NAMES: frozenset[str] = frozenset({".DS_Store"})
_GLOB_CHARACTERS: frozenset[str] = frozenset("*?[]!")


@dataclass(frozen=True, slots=True)
class WheelLayout:
    """Where the wheel's files come from, as absolute paths.

    Attributes:
        package: The package directory the wheel ships (``src/niwaki``).
        excludes: Paths left out of the wheel.  Each one is either a file or
            a directory whose whole subtree is excluded.
    """

    package: Path
    excludes: tuple[Path, ...] = ()


def read_wheel_layout(pyproject: Path = PYPROJECT) -> WheelLayout:
    """Read the wheel layout from a ``pyproject.toml``.

    Paths in the ``packages`` and ``exclude`` keys are relative to the
    directory that holds ``pyproject``, as Hatch reads them.

    Args:
        pyproject: The project file to read.  Defaults to the repository's
            own ``pyproject.toml``.

    Returns:
        The layout, with the package and every exclusion as absolute paths.

    Raises:
        FileNotFoundError: ``pyproject`` does not exist.
        ValueError: The ``[tool.hatch.build.targets.wheel]`` table is
            missing, ``packages`` does not name exactly one package, or an
            ``exclude`` entry is a glob or negation pattern.  Only plain
            paths are supported: a pattern would make this measure and the
            real wheel disagree without anyone noticing.

    Example:
        >>> layout = read_wheel_layout()
        >>> layout.package.name
        'niwaki'
    """
    with pyproject.open("rb") as handle:
        config = tomllib.load(handle)
    try:
        table = config["tool"]["hatch"]["build"]["targets"]["wheel"]
    except KeyError:
        raise ValueError(f"{pyproject}: no [tool.hatch.build.targets.wheel] table") from None
    packages: list[str] = table.get("packages", [])
    if len(packages) != 1:
        raise ValueError(f"{pyproject}: expected exactly one wheel package, found {packages!r}")
    excludes: list[str] = table.get("exclude", [])
    for entry in excludes:
        if _GLOB_CHARACTERS & set(entry):
            raise ValueError(
                f"{pyproject}: wheel exclude {entry!r} is a pattern; "
                "the weight measure only understands plain paths"
            )
    base = pyproject.resolve().parent
    return WheelLayout(
        package=base / packages[0],
        excludes=tuple(base / entry for entry in excludes),
    )


def _is_skipped(path: Path, package: Path) -> bool:
    """Whether a file under ``package`` never reaches the wheel by nature."""
    relative = path.relative_to(package)
    if _SKIPPED_DIRS & set(relative.parts[:-1]):
        return True
    return path.suffix in _SKIPPED_SUFFIXES or path.name in _SKIPPED_NAMES


def _is_excluded(path: Path, excludes: tuple[Path, ...]) -> bool:
    """Whether ``path`` is one of ``excludes`` or sits under one of them."""
    return any(path == exclude or path.is_relative_to(exclude) for exclude in excludes)


def shipped_files(layout: WheelLayout) -> list[Path]:
    """List the files of ``layout.package`` that the wheel ships.

    Args:
        layout: The wheel layout, typically from :func:`read_wheel_layout`.

    Returns:
        The shipped files, as absolute paths, sorted.  An empty package
        directory yields an empty list.

    Raises:
        FileNotFoundError: The package directory does not exist.
        NotADirectoryError: The package path is a file.
        ValueError: An exclusion does not exist.  A stale ``exclude`` entry
            in ``pyproject.toml`` means the layout this measure trusts no
            longer describes the tree, so it fails instead of guessing.

    Example:
        >>> files = shipped_files(read_wheel_layout())
        >>> any(path.name == "catalog.db" for path in files)
        True
    """
    package = layout.package
    if not package.exists():
        raise FileNotFoundError(f"package directory not found: {package}")
    if not package.is_dir():
        raise NotADirectoryError(f"package path is not a directory: {package}")
    for exclude in layout.excludes:
        if not exclude.exists():
            raise ValueError(f"wheel exclude matches nothing: {exclude}")
    return sorted(
        path
        for path in package.rglob("*")
        if path.is_file()
        and not _is_skipped(path, package)
        and not _is_excluded(path, layout.excludes)
    )


def shipped_weight(layout: WheelLayout) -> int:
    """Sum the apparent sizes (``st_size``) of the files the wheel ships.

    Args:
        layout: The wheel layout, typically from :func:`read_wheel_layout`.

    Returns:
        The total size in bytes.  ``0`` for an empty package directory.

    Raises:
        FileNotFoundError: The package directory does not exist.
        NotADirectoryError: The package path is a file.
        ValueError: An exclusion does not exist (see :func:`shipped_files`).

    Example:
        >>> shipped_weight(read_wheel_layout()) > 0
        True
    """
    return sum(path.stat().st_size for path in shipped_files(layout))
