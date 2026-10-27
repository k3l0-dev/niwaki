"""The cold-start budget — a gate, not just a benchmark.

``bench_imports.py`` *measures* import cost; nothing *enforced* it, so the
~90 ms cold-start that the SDK is designed around could regress silently.  The
one regression that matters is easy to cause and expensive: make ``import
niwaki`` eagerly pull the generated model tree (2,222 Pydantic classes, ~35 MB)
instead of leaving it lazy.  That turns a 90 ms import into hundreds of ms, and
no test would have noticed.

This asserts the entry point stays cheap.  The ceiling is deliberately generous
— the goal is to catch an order-of-change regression (eager-loading everything),
not to police a few milliseconds — so a slower CI runner does not flake it.  The
measurement is the *minimum* of several fresh subprocesses: the minimum is the
run least perturbed by scheduler noise and by first-run ``.pyc`` compilation.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

# The entry point most users reach for first.  It must NOT drag in the model
# tree — that laziness is the whole cold-start budget.
_ENTRY_POINT = "from niwaki import Niwaki, AsyncNiwaki"

# ~88 ms locally (min); a from-scratch design build is ~130 ms.  A regression
# that eager-loads the generated models lands in the hundreds.  250 ms leaves
# ~2.8x headroom over a local run so a loaded CI runner stays green while an
# order-of-change regression still trips the gate.
_BUDGET_MS = 250.0

_RUNS = 5


def _import_min_ms(statement: str, *, runs: int = _RUNS) -> float:
    """Return the fastest of *runs* fresh-interpreter imports, in milliseconds.

    Each run is a separate ``python -c`` subprocess, so nothing is cached in the
    parent process.  The minimum discards the first-run ``.pyc`` compilation and
    scheduler noise, leaving the steady-state cold-start cost.
    """
    best = float("inf")
    for _ in range(runs):
        start = time.perf_counter()
        completed = subprocess.run(
            [sys.executable, "-c", statement],
            capture_output=True,
            cwd=_ROOT,
            text=True,
        )
        elapsed_ms = (time.perf_counter() - start) * 1_000
        assert completed.returncode == 0, f"import failed:\n{completed.stderr}"
        best = min(best, elapsed_ms)
    return best


@pytest.mark.perf
def test_entry_point_cold_start_under_budget() -> None:
    """``import niwaki`` must stay cheap — the model tree stays lazy."""
    measured = _import_min_ms(_ENTRY_POINT)
    assert measured < _BUDGET_MS, (
        f"cold-start of {_ENTRY_POINT!r} was {measured:.0f} ms, over the "
        f"{_BUDGET_MS:.0f} ms budget. Something made the entry point eager — "
        "most likely an import that pulls the generated model tree at module "
        "load time. Keep it lazy."
    )


@pytest.mark.perf
def test_entry_point_does_not_import_the_model_tree() -> None:
    """The direct proof behind the budget: the models are absent after import.

    A timing ceiling catches the regression late (once it is already slow); this
    catches the *cause* — a generated model module resident in ``sys.modules``
    right after ``import niwaki`` means the tree is being eager-loaded.
    """
    probe = (
        "import sys; from niwaki import Niwaki; "
        "leaked = [m for m in sys.modules if m.startswith('niwaki.models._generated')]; "
        "print(len(leaked))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        cwd=_ROOT,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    leaked = int(completed.stdout.strip())
    assert leaked == 0, (
        f"{leaked} generated model module(s) were imported by `import niwaki` "
        "alone — the model tree must load lazily, on first use, not at import."
    )


@pytest.mark.perf
def test_entry_point_does_not_open_the_read_catalogue() -> None:
    """The read catalogue is lazy too: no ``query._catalog``, no ``sqlite3`` at import.

    The catalogue reader opens a multi-MB ``.db`` on first use; if ``import
    niwaki`` pulled it (or ``sqlite3``) in, every startup would pay for a
    subsystem most sessions never touch.  The absence of both modules right after
    import is the direct proof the reader stays lazy.
    """
    probe = (
        "import sys; from niwaki import Niwaki; "
        "watch = ('niwaki.catalog', 'niwaki.query._catalog', 'sqlite3'); "
        "leaked = [m for m in watch if m in sys.modules]; "
        "print(','.join(leaked))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, cwd=_ROOT, text=True
    )
    assert completed.returncode == 0, completed.stderr
    leaked = completed.stdout.strip()
    assert not leaked, (
        f"`import niwaki` pulled in {leaked} — the read catalogue must open "
        "lazily, on first use, never at import."
    )


@pytest.mark.perf
def test_entry_point_does_not_open_the_subscription_websocket() -> None:
    """The object-subscription WebSocket is lazy: no ``websockets`` at import.

    ``ApicSession.subscribe()`` opens a session-shared WebSocket on first use;
    ``websockets`` is imported only inside
    ``SubscriptionSocket._open_socket_locked``, never at module level, so a
    session that never subscribes never pays for it.
    """
    probe = (
        "import sys; from niwaki import Niwaki; leaked = 'websockets' in sys.modules; print(leaked)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, cwd=_ROOT, text=True
    )
    assert completed.returncode == 0, completed.stderr
    leaked = completed.stdout.strip()
    assert leaked == "False", (
        "`import niwaki` pulled in `websockets` — the subscription WebSocket "
        "must open lazily, on first use, never at import."
    )


@pytest.mark.perf
def test_async_entry_point_does_not_open_the_subscription_websocket() -> None:
    """Same guard as above, for the async entry point.

    ``AsyncSubscriptionSocket._connect`` lazy-imports
    ``websockets.asyncio.client`` the same way the sync socket does — closing
    the async import path explicitly, not just assuming symmetry with sync.
    """
    probe = (
        "import sys; from niwaki import AsyncNiwaki; "
        "leaked = 'websockets' in sys.modules; print(leaked)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, cwd=_ROOT, text=True
    )
    assert completed.returncode == 0, completed.stderr
    leaked = completed.stdout.strip()
    assert leaked == "False", (
        "`from niwaki import AsyncNiwaki` pulled in `websockets` — the async "
        "subscription WebSocket must open lazily, on first use, never at import."
    )


# ── The model import mirror (niwaki.models.<pkg>.<Cls>) ───────────────────────
# The public model path is a tree of ~3,000 generated re-export modules
# (src/niwaki/models/<pkg>/<Cls>.py) next to the canonical _generated tree.
# Its existence must cost `import niwaki` nothing, and an alias import must
# cost what the canonical import costs plus two tiny modules.

_MODELS_DIR = _ROOT / "src" / "niwaki" / "models"


def _mirror_top_level_packages() -> list[str]:
    """The mirror's top-level package names — derived from the generated tree."""
    generated = _MODELS_DIR / "_generated"
    return sorted(
        child.name
        for child in generated.iterdir()
        if child.is_dir() and child.name != "__pycache__"
    )


@pytest.mark.perf
@pytest.mark.parametrize("entry", ["import niwaki", "from niwaki import Niwaki, AsyncNiwaki"])
def test_entry_point_does_not_import_the_model_mirror(entry: str) -> None:
    """No mirror module is resident in ``sys.modules`` right after the entry point."""
    tops = _mirror_top_level_packages()
    assert "fv" in tops and "enums" in tops, f"mirror package derivation is broken: {tops[:5]}"
    probe = (
        f"import sys; {entry}; "
        f"tops = {tuple(tops)!r}; "
        "leaked = sorted(m for m in sys.modules if m.startswith('niwaki.models.') "
        "and m.split('.')[2] in tops); "
        "print(','.join(leaked))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, cwd=_ROOT, text=True
    )
    assert completed.returncode == 0, completed.stderr
    leaked = completed.stdout.strip()
    assert not leaked, (
        f"`{entry}` imported model-mirror module(s): {leaked} — the mirror must "
        "load on first use of a public model path, never at import."
    )


# An alias import loads the canonical module plus two small mirror modules
# (package __init__ + leaf re-export).  An eager mirror package (importing every
# class of the package up front) would add tens of milliseconds and trip this.
_ALIAS_OVERHEAD_MS = 20.0
_ALIAS_OVERHEAD_RATIO = 1.10


@pytest.mark.perf
def test_alias_model_import_costs_what_the_canonical_import_costs() -> None:
    canonical = _import_min_ms("from niwaki.models._generated.fv.fvBD import fvBD")
    alias = _import_min_ms("from niwaki.models.fv.fvBD import fvBD")
    ceiling = max(canonical + _ALIAS_OVERHEAD_MS, canonical * _ALIAS_OVERHEAD_RATIO)
    assert alias < ceiling, (
        f"alias import took {alias:.0f} ms vs {canonical:.0f} ms for the canonical "
        f"path (ceiling {ceiling:.0f} ms). The mirror is supposed to add two tiny "
        "modules, not load a package eagerly."
    )


# ── The typed navigation stubs (niwaki._generated_nav) ────────────────────────
# The typed navigation classes exist for the type checker only: .pyi stubs, no
# runtime module.  Nothing may import them — neither the entry point nor a
# navigation — or the cold-start budget would pay for ~1,800 classes that do
# nothing at runtime.  (A stub-only directory still imports as an empty
# namespace package, so the guard looks at sys.modules, not at ImportError.)

_SRC = _ROOT / "src" / "niwaki"


@pytest.mark.perf
@pytest.mark.parametrize(
    "entry",
    [
        "import niwaki",
        "from niwaki import Niwaki, AsyncNiwaki",
        # Navigation itself: sync and async chains, the explicit root, a typo.
        "from niwaki import Niwaki, AsyncNiwaki; a = Niwaki(); "
        "a.tenant('x').bd('y').subnet(subnet='10.0.0.1/24').read; a.root; "
        "b = AsyncNiwaki(); b.tenant('x').bd('y'); b.root",
    ],
)
def test_nothing_imports_the_navigation_stubs(entry: str) -> None:
    probe = (
        f"import sys; {entry}; "
        "leaked = sorted(m for m in sys.modules if m.startswith('niwaki._generated_nav')); "
        "print(','.join(leaked))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, cwd=_ROOT, text=True
    )
    assert completed.returncode == 0, completed.stderr
    leaked = completed.stdout.strip()
    assert not leaked, (
        f"`{entry}` imported {leaked} — the navigation stubs are for type "
        "checkers only and must never be imported at runtime."
    )


def _imports_of(tree: ast.Module, module: str) -> list[ast.stmt]:
    return [
        node
        for node in ast.walk(tree)
        if (isinstance(node, ast.ImportFrom) and (node.module or "").startswith(module))
        or (
            isinstance(node, ast.Import)
            and any(alias.name.startswith(module) for alias in node.names)
        )
    ]


def _type_checking_bodies(tree: ast.Module) -> set[int]:
    """``id`` of every statement nested in an ``if TYPE_CHECKING:`` body."""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and ast.unparse(node.test) in {
            "TYPE_CHECKING",
            "typing.TYPE_CHECKING",
        }:
            for statement in node.body:
                guarded |= {id(inner) for inner in ast.walk(statement)}
    return guarded


@pytest.mark.perf
def test_source_imports_the_navigation_stubs_only_for_the_type_checker() -> None:
    """Every ``niwaki._generated_nav`` import in the package sits under ``TYPE_CHECKING``."""
    offenders: list[str] = []
    sources = [
        path
        for path in sorted(_SRC.rglob("*.py"))
        # The generated model trees (thousands of files) never import the stubs.
        if not (
            path.relative_to(_SRC).parts[0] == "models" and len(path.parts) > len(_SRC.parts) + 2
        )
    ]
    for path in sources:
        text = path.read_text(encoding="utf-8")
        if "_generated_nav" not in text:
            continue
        tree = ast.parse(text, filename=str(path))
        guarded = _type_checking_bodies(tree)
        offenders += [
            f"{path.relative_to(_ROOT)}:{node.lineno}"
            for node in _imports_of(tree, "niwaki._generated_nav")
            if id(node) not in guarded
        ]
    assert not offenders, f"runtime import(s) of the navigation stubs: {offenders}"
