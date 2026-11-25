"""The gate scripts must keep running the wheel smoke.

The 2.0.0 release lesson: ``scripts/wheel_smoke.py`` ran only on the public
CI, so an assertion pinned to the 1.x naming shipped unnoticed and reddened
the public release run (fixed by ``2ca494e0`` + retag).  The structural fix
is that the private gates run the smoke themselves — ``checks.sh`` on every
full gate, ``release_public.sh`` in its preflight — and this guard pins that
wiring so it cannot silently fall out again.  The same step weighs the built
wheel (``scripts/package_weight.py``), so the archive's size budget is pinned
the same way.

The gate scripts are private tooling (excluded from the public export), so
these checks only run where they exist.
"""

from __future__ import annotations

import pathlib
import re

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


def test_release_preflight_runs_the_canary_gitleaks() -> None:
    """The export runs the canary's own secret scanner on the staged tree.

    The canary audits the public history with gitleaks, whose rules differ
    from the preflight's detect-secrets; without this step a gitleaks finding
    only surfaced a week after the release.  The version must come from
    ``canary.yml`` (one pin, never a second copy that drifts), the scan must
    use the shipped ``.gitleaks.toml``, and a finding must abort the export.
    """
    text = (_SCRIPTS / "release_public.sh").read_text(encoding="utf-8")
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    pin_lines = [line for line in code if "canary.yml" in line]
    assert pin_lines, "release_public.sh no longer reads the gitleaks pin from canary.yml"
    scans = [line for line in code if "gitleaks dir" in line]
    assert scans, "release_public.sh no longer runs `gitleaks dir` on the staged tree"
    scan = scans[0]
    assert '"$STAGE"' in scan, f"gitleaks does not scan the staged tree: {scan.strip()!r}"
    assert ".gitleaks.toml" in scan, f"gitleaks does not use the shipped config: {scan.strip()!r}"
    assert "|| true" not in scan, "the gitleaks scan must be able to abort the export"
    assert "if ! gitleaks dir" in scan, "a gitleaks finding no longer aborts the export"
    # The scan happens before anything is cloned or pushed.
    assert text.index("gitleaks dir") < text.index("git clone"), (
        "gitleaks runs after publishing starts"
    )


def test_canary_pins_the_gitleaks_version_the_preflight_reads() -> None:
    """The pin the preflight greps for is still there, in the expected form."""
    canary = (_SCRIPTS.parent / ".github" / "workflows" / "canary.yml").read_text(encoding="utf-8")
    assert re.search(r"gitleaks/releases/download/v\d+\.\d+\.\d+/", canary), (
        "canary.yml no longer pins gitleaks as releases/download/vX.Y.Z/ — update release_public.sh"
    )


def _wheel_smoke_body(text: str) -> str:
    """The body of the ``run_wheel_smoke`` shell function in ``checks.sh``."""
    match = re.search(r"^run_wheel_smoke\(\) \{\n(.*?)^\}", text, flags=re.MULTILINE | re.DOTALL)
    assert match, "checks.sh no longer defines run_wheel_smoke() { ... }"
    return match.group(1)


def test_wheel_smoke_step_checks_the_wheel_weight() -> None:
    """The step that builds the wheel also weighs it, against its budgets.

    Inside ``run_wheel_smoke``, so both the full gate and ``--wheel-smoke``
    (the export preflight) refuse a wheel that grew past its budget.
    """
    text = (_SCRIPTS / "checks.sh").read_text(encoding="utf-8")
    body = _wheel_smoke_body(text)
    assert "scripts/package_weight.py" in body, (
        "run_wheel_smoke no longer calls scripts/package_weight.py on the built wheel"
    )
    call = next(line for line in body.splitlines() if "scripts/package_weight.py" in line)
    assert ".whl" in call, f"the weight check is not given the built wheel: {call.strip()!r}"
    assert not call.lstrip().startswith("#"), "the weight check is commented out"
    assert "|| true" not in call, "the weight check must be able to fail the gate"


def test_fast_gate_runs_the_unasync_freshness_check() -> None:
    """The pre-commit hook (``checks.sh --fast``) refuses a stale generated twin.

    The sync query twins are generated from their async sources and committed
    (``niwaki._codegen.unasync``).  The check is corpus-free and fast, so it
    belongs to the static gate: an async edit committed without
    ``unasync --write``, or a hand edit of a generated file, is refused at
    commit time — not after the 15-minute suite.
    """
    text = (_SCRIPTS / "checks.sh").read_text(encoding="utf-8")
    lines = text.splitlines()
    fast_exit = next(
        (i for i, line in enumerate(lines) if re.match(r'\s*if \[\[ "\$FAST" -eq 1 \]\]', line)),
        None,
    )
    assert fast_exit is not None, "checks.sh lost its --fast early exit"
    calls = [
        (i, line)
        for i, line in enumerate(lines)
        if "unasync" in line and "--check" in line and not line.lstrip().startswith("#")
    ]
    assert calls, "checks.sh no longer runs `python -m niwaki._codegen.unasync --check`"
    index, call = calls[0]
    assert "niwaki._codegen.unasync" in call or "_codegen/unasync.py" in call, call.strip()
    assert index < fast_exit, "the unasync check sits behind the --fast early exit"
    assert "|| true" not in call, "the unasync check must be able to fail the gate"
    assert "--write" not in call, "the gate must check, never rewrite"


def _regen_verify_block() -> list[str]:
    """The non-comment lines of the ``--verify`` block of ``regen.sh``."""
    lines = (_SCRIPTS / "regen.sh").read_text(encoding="utf-8").splitlines()
    start = next(
        (i for i, line in enumerate(lines) if re.match(r'\s*if \[ "\$VERIFY" -eq 1 \]', line)),
        None,
    )
    assert start is not None, "regen.sh lost its --verify block"
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "fi")
    return [line for line in lines[start:end] if not line.lstrip().startswith("#")]


def test_regen_verify_runs_the_exhaustive_node_probe() -> None:
    """``regen.sh --verify`` proves every model's typed-navigation target exists.

    Each generated model names its typed navigation class under
    ``TYPE_CHECKING`` (an import of ``niwaki._generated_nav.<pkg>``); a regen
    that drops or renames a stub class leaves a dangling import that no
    runtime test can see.  The only guard is the exhaustive ``node(dn, C)``
    probe of ``tests/typing/test_node_static.py`` (mypy and pyright over
    every generated class) — so the regeneration pipeline's own verify step
    must run it, not just the full gate.
    """
    block = "\n".join(_regen_verify_block())
    assert "tests/typing/test_node_static.py" in block, (
        "regen.sh --verify does not run tests/typing/test_node_static.py"
    )
    # Neither deselected nor narrowed away from the exhaustive probes.
    for line in _regen_verify_block():
        if "test_node_static.py" in line:
            assert "--deselect" not in line and "not every_generated" not in line, line.strip()
