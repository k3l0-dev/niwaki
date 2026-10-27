"""``scripts/wheel_smoke.py`` proves the model import mirror ships in the wheel.

The public model path (``from niwaki.models.fv.fvBD import fvBD``) is served
by real generated re-export modules, not by an import hook.  The wheel smoke
runs against a pristine venv with the built wheel installed (``checks.sh``
full gate, ``release_public.sh`` preflight), so it is the place that proves
those ~3,000 mirror files are actually packaged.  For that proof to mean
anything the smoke must:

1. import a public alias path, **without** a ``pyright: ignore`` crutch — the
   import is statically resolvable now, and the script is type-checked;
2. refuse to pass when a niwaki ``MetaPathFinder`` is in ``sys.meta_path`` —
   otherwise a revived hook could serve the alias and hide a packaging slip
   that dropped the mirror files.  A finder is "niwaki's" when its class's
   ``__module__`` is ``niwaki`` or starts with ``niwaki.``.

These tests drive the script itself with the repo interpreter (the module
under test is the script, not the wheel).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SMOKE = _REPO / "scripts" / "wheel_smoke.py"

if not _SMOKE.is_file():
    pytest.skip("scripts/wheel_smoke.py not present in this distribution", allow_module_level=True)

# Plants a do-nothing finder that *claims* to be niwaki's, then runs the smoke
# as ``__main__``.  The finder serves nothing, so only the smoke's own check
# can make the run fail.
_PLANTED_FINDER = """
import runpy
import sys
from importlib.abc import MetaPathFinder


class _GeneratedAlias(MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        return None


_GeneratedAlias.__module__ = {module!r}
sys.meta_path.insert(0, _GeneratedAlias())
runpy.run_path({script!r}, run_name="__main__")
"""


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, cwd=_REPO, text=True, timeout=120
    )


def test_smoke_passes_on_the_repo_install() -> None:
    """Nominal: the real package (no hook) satisfies the smoke."""
    completed = subprocess.run(
        [sys.executable, str(_SMOKE)], capture_output=True, cwd=_REPO, text=True, timeout=120
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "wheel smoke OK" in completed.stdout


@pytest.mark.parametrize("module", ["niwaki.models", "niwaki"])
def test_smoke_refuses_a_niwaki_import_hook(module: str) -> None:
    """A niwaki finder in ``sys.meta_path`` must fail the smoke loudly."""
    completed = _run(_PLANTED_FINDER.format(module=module, script=str(_SMOKE)))
    assert completed.returncode != 0, (
        "wheel_smoke.py passed with a niwaki MetaPathFinder installed — it must "
        "assert that the alias path is served by real files, not an import hook"
    )
    assert "wheel smoke OK" not in completed.stdout


def test_smoke_ignores_foreign_finders() -> None:
    """Edge: a finder from another package (e.g. a coverage or editable hook) is fine."""
    completed = _run(_PLANTED_FINDER.format(module="somebody_else.hooks", script=str(_SMOKE)))
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_smoke_alias_import_is_not_silenced_for_the_type_checker() -> None:
    """The alias import resolves statically now: no ``pyright: ignore`` crutch."""
    lines = _SMOKE.read_text(encoding="utf-8").splitlines()
    alias_imports = [line for line in lines if line.startswith("from niwaki.models.fv.fvBD import")]
    assert alias_imports, "wheel_smoke.py no longer imports a public model alias path"
    for line in alias_imports:
        assert "pyright: ignore" not in line and "type: ignore" not in line, line


def test_smoke_no_longer_describes_a_meta_path_finder_as_the_mechanism() -> None:
    text = _SMOKE.read_text(encoding="utf-8")
    assert "resolved at runtime by a MetaPathFinder" not in text


# ── Typed navigation stubs ship in the wheel ──────────────────────────────────
# The smoke inspects the *installed* package directory; its checker is a plain
# function of that directory, driven here on scratch trees.


def _smoke_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_wheel_smoke_under_test", _SMOKE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _nav_stub_problems(package_dir: Path) -> list[str]:
    checker = cast(Callable[[Path], list[str]], _smoke_module().nav_stub_problems)
    return checker(package_dir)


def _package(tmp_path: Path, stubs: set[str], *, typed: bool = True) -> Path:
    package = tmp_path / "niwaki"
    (package / "_generated_nav").mkdir(parents=True)
    if typed:
        (package / "py.typed").write_text("", encoding="utf-8")
    for name in stubs:
        (package / "_generated_nav" / name).write_text("# stub\n", encoding="utf-8")
    return package


_REQUIRED = {"__init__.pyi", "_base.pyi", "_root.pyi", "fv.pyi"}


def test_nav_check_accepts_a_complete_stub_package(tmp_path: Path) -> None:
    assert _nav_stub_problems(_package(tmp_path, _REQUIRED | {"infra.pyi"})) == []


def test_nav_check_accepts_the_repo_package() -> None:
    """The source tree is what the wheel packs: it must satisfy the smoke."""
    import niwaki

    assert _nav_stub_problems(Path(niwaki.__file__).resolve().parent) == []


@pytest.mark.parametrize("dropped", sorted(_REQUIRED))
def test_nav_check_refuses_a_missing_stub(tmp_path: Path, dropped: str) -> None:
    problems = _nav_stub_problems(_package(tmp_path, _REQUIRED - {dropped}))
    assert any(dropped in problem for problem in problems), problems


def test_nav_check_refuses_a_missing_stub_directory(tmp_path: Path) -> None:
    package = tmp_path / "niwaki"
    package.mkdir()
    (package / "py.typed").write_text("", encoding="utf-8")
    assert _nav_stub_problems(package)


def test_nav_check_refuses_a_runtime_module_in_the_stub_package(tmp_path: Path) -> None:
    package = _package(tmp_path, _REQUIRED | {"__init__.py"})
    assert any(".py" in problem for problem in _nav_stub_problems(package))


def test_nav_check_refuses_an_untyped_package(tmp_path: Path) -> None:
    package = _package(tmp_path, _REQUIRED, typed=False)
    assert any("py.typed" in problem for problem in _nav_stub_problems(package))
