"""The fault-code models import fast once their register is shared.

``faultInst`` (and four sibling models) used to inline the same 80,512-name
fault-code register twice — a ``Literal`` and a ``named_number`` dict, ~5 MB
of source.  Importing one cost ~550 ms (compile, then a pydantic schema over
an 80k-member ``Literal``) and a transient peak of ~240 MB, for anyone who
merely read a fault.  With the register shared and encoded compactly, the
import is a small model plus one compact module.

The measure isolates the model: a fresh ``-B`` interpreter (no bytecode
written by the probe) first imports the shared machinery (``models.base``,
``models._wire``), then times the ``faultInst`` import alone, register
included.  The minimum of a few runs discards scheduler noise.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

_MODULE = "niwaki.models._generated.fault.faultInst"

#: ~550 ms with the register inlined (2026-10-26); the shared register was
#: measured at ~35 ms to load.  150 ms keeps headroom for a loaded CI runner
#: while any return of an inlined register (hundreds of ms) still trips it.
_BUDGET_MS = 150.0

_RUNS = 3

_PROBE = f"""
import importlib, time
import niwaki.models.base, niwaki.models._wire
start = time.perf_counter()
importlib.import_module({_MODULE!r})
print((time.perf_counter() - start) * 1000)
"""


def _import_min_ms() -> float:
    best = float("inf")
    for _ in range(_RUNS):
        completed = subprocess.run(
            [sys.executable, "-B", "-c", _PROBE],
            capture_output=True,
            cwd=_ROOT,
            text=True,
        )
        assert completed.returncode == 0, f"import failed:\n{completed.stderr}"
        best = min(best, float(completed.stdout.strip()))
    return best


@pytest.mark.perf
def test_fault_inst_cold_import_under_budget() -> None:
    measured = _import_min_ms()
    assert measured < _BUDGET_MS, (
        f"importing {_MODULE} took {measured:.0f} ms, over the {_BUDGET_MS:.0f} ms "
        "budget. The fault-code register is most likely inlined again instead of "
        "shared (models/_generated/_registers.py)."
    )
