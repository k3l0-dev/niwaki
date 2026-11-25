"""The cold-start budget — a gate, not just a benchmark.

``bench_imports.py`` *measures* import cost; nothing *enforced* it, so the
cold-start that the SDK is designed around could regress silently.  The one
regression that matters is easy to cause and expensive: make ``import niwaki``
eagerly pull the generated model tree (2,222 Pydantic classes, ~35 MB) instead
of leaving it lazy.  That turns a ~115 ms import into ~2.7 s, and no test would
have noticed.

Why the budget is relative
--------------------------
An absolute millisecond budget measures the machine as much as the code.  On an
Apple-silicon laptop the very same ``import niwaki`` takes ~115 ms on a
performance core and 450-500 ms once the scheduler parks the process on the
efficiency cores (QoS "background"/utility — what a long pytest run under load
or a ``taskpolicy -b`` shell gets).  A fixed 250 ms ceiling therefore went red
under load without any code change, and taking the minimum of several runs
cannot help when *every* run lands on slow cores.

What does *not* move with the core class is the ratio between ``import
niwaki`` and the import of its own dependencies (``pydantic``, ``httpx``,
``stamina`` — the floor no amount of SDK work can go below): niwaki costs ~1.7x
the floor on performance cores and ~1.5x on efficiency cores.  So the gate
measures both in the *same* test, interleaved (floor, niwaki, floor, niwaki,
...) with the same interpreter and flags so both halves of a pair see the same
machine state, and asserts ``niwaki <= RATIO * floor`` with ``RATIO = 2.5``
(~46% headroom over the measured 1.7).

Two estimators of that ratio are computed from the same samples — the ratio of
the fastest run of each stream, and the median of the per-pair ratios — and the
gate takes the *lower*.  They fail in different ways (the first is thrown by one
lucky-fast run in a single stream, the second by cores changing between the two
halves of a pair), so a false alarm needs both to be wrong at once, while a real
regression raises every sample and therefore both.

What it still catches: anything that adds about +50 ms to a ~115 ms import
(+45%) trips the ratio — eagerly importing a single generated package (``fv``,
+115 ms) measures 3.4x the floor — and the regression it exists for,
eager-loading the whole generated model tree (~24x a normal import, ~34x the
floor), trips it by an order of magnitude.  A few milliseconds of extra import
(``yaml``, ``sqlite3``, ``websockets``) is below what any timing can tell from
noise: the ``sys.modules`` guards below prove that *cause* directly, whatever
the clock says.

An absolute ceiling (``_SANITY_CEILING_MS``) is kept as a last line of defence,
far from anything noise can produce: it only fires when the *floor* is
inflated together with niwaki so the ratio stays flat, and it sits ~3x above the
slowest efficiency-core measurement and well below the ~2.7 s eager-model
import.
"""

from __future__ import annotations

import ast
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

# The entry point most users reach for first.  It must NOT drag in the model
# tree — that laziness is the whole cold-start budget.
_ENTRY_POINT = "from niwaki import Niwaki, AsyncNiwaki"

# What `import niwaki` cannot be cheaper than: its own mandatory dependencies,
# imported eagerly.  (`pyyaml` and `websockets` are lazy and absent here.)
_FLOOR = "import pydantic, httpx, stamina"

# niwaki / floor measures ~1.7 on performance cores and ~1.5 on efficiency
# cores.  2.5 leaves ~46% headroom for noise and slower CI runners while an
# eager-model regression (~24x) is missed by an order of magnitude.
_RATIO = 2.5

# Interleaved (floor, niwaki) pairs.  Seven keeps the median meaningful (it
# tolerates three bad pairs) and the test short: ~1.3 s on a performance core.
_PAIRS = 7

# Absolute last line of defence, far above anything noise produces: the slowest
# measured niwaki import (efficiency cores, loaded) is ~500 ms, the eager-model
# regression is ~2,700 ms.  1,500 ms cannot flake and still separates them.
_SANITY_CEILING_MS = 1_500.0


def _fresh_import_ms(statement: str) -> float:
    """Wall-clock time, in milliseconds, of ``python -c <statement>`` in a fresh process.

    The interpreter is ``sys.executable`` with no extra flags, so every
    measurement in this module is taken under the same conditions.
    """
    start = time.perf_counter()
    completed = subprocess.run(
        [sys.executable, "-c", statement],
        capture_output=True,
        cwd=_ROOT,
        text=True,
    )
    elapsed_ms = (time.perf_counter() - start) * 1_000
    assert completed.returncode == 0, f"import failed:\n{completed.stderr}"
    return elapsed_ms


def _interleaved_import_ms(
    baseline: str, candidate: str, *, pairs: int = _PAIRS
) -> tuple[list[float], list[float]]:
    """Time *baseline* and *candidate* alternately, ``baseline`` first in each pair.

    Returns the two sample lists (milliseconds, same length).  Alternating — A,
    B, A, B, ... — rather than running all of A then all of B means both streams
    sample the same stretch of machine state (core class, thermal state, CI
    neighbours), which is what makes their ratio meaningful.
    """
    baseline_ms: list[float] = []
    candidate_ms: list[float] = []
    for _ in range(pairs):
        baseline_ms.append(_fresh_import_ms(baseline))
        candidate_ms.append(_fresh_import_ms(candidate))
    return baseline_ms, candidate_ms


def _calibrated_ratio(baseline_ms: list[float], candidate_ms: list[float]) -> float:
    """Estimate ``candidate / baseline`` from interleaved samples, robustly.

    The lower of two estimators computed from the same samples: the ratio of the
    fastest run of each stream, and the median of the per-pair ratios.  See the
    module docstring for why a false alarm needs both to be wrong at once while
    a real regression raises both.
    """
    ratio_of_minima = min(candidate_ms) / min(baseline_ms)
    median_pair_ratio = statistics.median(
        candidate / baseline for baseline, candidate in zip(baseline_ms, candidate_ms, strict=True)
    )
    return min(ratio_of_minima, median_pair_ratio)


@pytest.mark.perf
def test_entry_point_cold_start_is_proportionate_to_its_dependencies() -> None:
    """``import niwaki`` must cost at most ``_RATIO`` times its dependency floor.

    The floor (``import pydantic, httpx, stamina``) and the niwaki import are
    timed interleaved in fresh interpreters, so the assertion holds on any
    machine, on any core class, under any load that slows both equally — and
    still fails when niwaki itself gets heavier (an eager model tree, a
    heavyweight module-level import).
    """
    floor_ms, niwaki_ms = _interleaved_import_ms(_FLOOR, _ENTRY_POINT)
    ratio = _calibrated_ratio(floor_ms, niwaki_ms)
    assert ratio <= _RATIO, (
        f"cold-start of {_ENTRY_POINT!r} is {ratio:.2f}x the cost of its own "
        f"dependencies ({_FLOOR!r}), over the {_RATIO}x budget "
        f"(fastest runs: niwaki {min(niwaki_ms):.0f} ms vs floor "
        f"{min(floor_ms):.0f} ms; niwaki samples "
        f"{[round(x) for x in niwaki_ms]} ms, floor samples "
        f"{[round(x) for x in floor_ms]} ms). Something made the entry point "
        "heavier — most likely an import that pulls the generated model tree "
        "(or another heavy module) at load time. Keep it lazy."
    )
    assert min(niwaki_ms) < _SANITY_CEILING_MS, (
        f"cold-start of {_ENTRY_POINT!r} was {min(niwaki_ms):.0f} ms at best, over "
        f"the {_SANITY_CEILING_MS:.0f} ms sanity ceiling — even if the ratio to "
        "its dependencies looks fine, an import this slow is a regression."
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
# (package __init__ + leaf re-export): it measures ~1.00x the canonical import,
# and at most ~1.06x on efficiency cores.  An eager mirror package (importing
# every class of the package up front) measures ~2.1x.  Same calibration as the
# entry-point budget — interleaved, ratio, no absolute milliseconds — because
# sequential "all canonical, then all alias" runs flaked under background QoS
# when the two halves landed on different core classes.
_ALIAS_RATIO = 1.4


@pytest.mark.perf
def test_alias_model_import_costs_what_the_canonical_import_costs() -> None:
    canonical_ms, alias_ms = _interleaved_import_ms(
        "from niwaki.models._generated.fv.fvBD import fvBD",
        "from niwaki.models.fv.fvBD import fvBD",
    )
    ratio = _calibrated_ratio(canonical_ms, alias_ms)
    assert ratio <= _ALIAS_RATIO, (
        f"alias import costs {ratio:.2f}x the canonical import (budget "
        f"{_ALIAS_RATIO}x; fastest runs: alias {min(alias_ms):.0f} ms vs canonical "
        f"{min(canonical_ms):.0f} ms). The mirror is supposed to add two tiny "
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
