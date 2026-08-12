"""The gate scripts must keep running the wheel smoke.

The 2.0.0 release lesson: ``scripts/wheel_smoke.py`` ran only on the public
CI, so an assertion pinned to the 1.x naming shipped unnoticed and reddened
the public release run (fixed by ``2ca494e0`` + retag).  The structural fix
is that the private gates run the smoke themselves — ``checks.sh`` on every
full gate, ``release_public.sh`` in its preflight — and this guard pins that
wiring so it cannot silently fall out again.

The gate scripts are private tooling (excluded from the public export), so
these checks only run where they exist.
"""

from __future__ import annotations

import pathlib

import pytest

_SCRIPTS = pathlib.Path(__file__).parents[2] / "scripts"

if not (_SCRIPTS / "checks.sh").is_file():
    pytest.skip("private gate scripts not present in this distribution", allow_module_level=True)


def test_checks_full_gate_runs_the_wheel_smoke() -> None:
    """The full gate builds the wheel and runs the smoke before the tests."""
    text = (_SCRIPTS / "checks.sh").read_text(encoding="utf-8")
    assert "wheel_smoke.py" in text, "checks.sh no longer runs scripts/wheel_smoke.py"
    assert "run_wheel_smoke" in text, "the wheel-smoke step lost its single definition"
    # The step must not sit behind the --fast early exit only: the function is
    # invoked at least twice (the --wheel-smoke mode and the full-gate call).
    assert text.count("run_wheel_smoke") >= 3, (
        "run_wheel_smoke is defined but no longer called from the full gate"
    )


def test_release_preflight_runs_the_wheel_smoke() -> None:
    """The export preflight refuses a stale smoke at the source."""
    text = (_SCRIPTS / "release_public.sh").read_text(encoding="utf-8")
    assert "--wheel-smoke" in text, "release_public.sh preflight no longer runs the wheel smoke"
