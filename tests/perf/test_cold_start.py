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
