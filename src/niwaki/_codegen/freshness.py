"""The regen manifest — freshness made verifiable without the corpus.

The rebuild-and-compare guards (``test_child_map_fresh``, ``test_shipped_db_is_fresh``,
``test_generated_fresh``…) prove the committed artifacts match the schema
corpus — and skip wherever the 1.7 GB corpus is absent, which is everywhere
but the dev machine.  This module closes that hole with a weaker but
corpus-free invariant:

    **nothing moved since the last regeneration** — neither the generators
    and their inputs, nor the artifacts they emitted.

At regen time (``scripts/regen.sh``), :func:`write_manifest` records a SHA-256
fingerprint of every generator source, template, shared naming/type policy,
the curated vocabulary, and every generated artifact.  The corpus-free guard
(``tests/test_freshness_manifest.py``) recomputes both sides and fails on any
divergence: a generator edited without a regen, an artifact edited by hand,
or a regen that forgot to refresh the manifest.  Together with the corpus
guards on the dev side, the chain is closed end to end.

Inputs are **glob-driven** so a new generator or template cannot dodge the
manifest by not being listed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

_CODEGEN = Path(__file__).resolve().parent
_SRC = _CODEGEN.parent  # src/niwaki
MANIFEST_PATH = _CODEGEN / "regen_manifest.json"


def _input_files() -> list[Path]:
    """Everything whose change should force a regeneration."""
    files = [
        *sorted(_CODEGEN.glob("*.py")),
        *sorted((_CODEGEN / "templates").glob("*")),
        *sorted((_SRC / "_schema").glob("*.py")),
        _SRC / "domain" / "vocabulary.yaml",
    ]
    return [f for f in files if f.is_file() and f.name != "regen_manifest.json"]


def _private_input_files() -> list[Path]:
    """Inputs that never ship publicly, guarded only where they exist.

    The extraction pipeline (``data/scripts``) decides the Python type of
    every generated model, but ``data/`` stays private — recording it in the
    main ``inputs`` table would fail the public guard on files the export
    deliberately omits.  This table is verified with skip-if-absent
    semantics instead: on the dev machine an edited extractor without a
    regen fails loudly; on the public side there is nothing to check.
    """
    repo = _SRC.parent.parent
    return sorted(f for f in (repo / "data" / "scripts").glob("*.py") if f.is_file())


def _artifact_files() -> list[Path]:
    """Everything a regeneration emits."""
    repo = _SRC.parent.parent
    return [
        *sorted((_SRC / "models" / "_generated").rglob("*.py")),
        _SRC / "domain" / "_child_map.py",
        *sorted((_SRC / "design" / "_generated_cursors").rglob("*.py")),
        _SRC / "query" / "_catalog" / "catalog.db",
        *sorted((repo / "docs" / "reference" / "vocabulary").rglob("*.md")),
        # Emitted by regen (scripts/regen.sh), committed, and shipped with
        # the public tests: a hand edit — or a regen that died before this
        # write — must fail the guard, not parametrise the whole model suite
        # from doctored fixtures.
        repo / "tests" / "models" / "_test_data.json",
    ]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compute() -> dict[str, dict[str, str]]:
    """The current fingerprint of inputs and artifacts, repo-relative keys."""
    root = _SRC.parent.parent  # repo root

    def _table(files: list[Path]) -> dict[str, str]:
        return {str(f.relative_to(root)): _digest(f) for f in files}

    return {
        "inputs": _table(_input_files()),
        "private_inputs": _table(_private_input_files()),
        "artifacts": _table(_artifact_files()),
    }


def write_manifest() -> None:
    """Record the current state as the last-regeneration fingerprint.

    Called by ``scripts/regen.sh`` after the generators and the formatter have
    run — never by hand after editing something, which would defeat the guard.
    """
    MANIFEST_PATH.write_text(
        json.dumps(compute(), indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )


def verify() -> list[str]:
    """Compare the repo against the committed manifest.

    Returns:
        Human-readable divergences, empty when everything still matches.  A
        missing manifest is itself a divergence — the guard must never pass
        by absence.
    """
    if not MANIFEST_PATH.is_file():
        return ["regen manifest missing: run scripts/regen.sh (or freshness.write_manifest)"]
    recorded = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    current = compute()
    problems: list[str] = []
    for side in ("inputs", "artifacts"):
        was, now = recorded.get(side, {}), current[side]
        for path in sorted(set(was) - set(now)):
            problems.append(f"{side}: {path} recorded but no longer present")
        for path in sorted(set(now) - set(was)):
            problems.append(f"{side}: {path} present but not recorded (regen forgotten?)")
        for path in sorted(set(was) & set(now)):
            if was[path] != now[path]:
                problems.append(f"{side}: {path} changed since the last regeneration")
    # Private inputs never ship: absence is expected off the dev machine, so
    # only files that exist are compared — a divergence still fails loudly.
    was, now = recorded.get("private_inputs", {}), current["private_inputs"]
    for path in sorted(set(now) - set(was)):
        problems.append(f"private_inputs: {path} present but not recorded (regen forgotten?)")
    for path in sorted(set(was) & set(now)):
        if was[path] != now[path]:
            problems.append(f"private_inputs: {path} changed since the last regeneration")
    return problems


if __name__ == "__main__":
    import sys

    if "--write" in sys.argv:
        write_manifest()
        print(f"regen manifest written: {MANIFEST_PATH}")
    else:
        issues = verify()
        for issue in issues:
            print(issue, file=sys.stderr)
        sys.exit(1 if issues else 0)
