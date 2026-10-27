"""The committed sync query twins are exactly what the generator makes.

``src/niwaki/query/_builder.py`` and ``src/niwaki/query/_subscription.py`` are
**generated** from their async sources by ``niwaki._codegen.unasync`` (API
contract: ``tests/test_unasync.py``) and committed.  A hand edit of a
generated file, or an async edit without a regeneration, is caught here:
each target is regenerated in memory, passed through ``ruff format`` with the
repository configuration, and compared byte for byte with the committed file.

The guard is corpus-free — it reads two source files — so, by decision, it
lives **outside** the regen freshness manifest (which needs the schema corpus
to regenerate, ``scripts/regen.sh``) and runs in ``scripts/checks.sh --fast``
(the pre-commit hook) as ``unasync --check``.  The generated files stay
measured by coverage: they are the code users run.

Fix a red test with ``uv run python -m niwaki._codegen.unasync --write`` after
editing the *async* source — never by editing the generated file.
"""

from __future__ import annotations

import fnmatch
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from niwaki._codegen import freshness, unasync
from tests.test_twin_parity import PAIRS as PARITY_PAIRS

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "niwaki"

QUERY_PAIRS = (
    ("query/_async_builder.py", "query/_builder.py"),
    ("query/_async_subscription.py", "query/_subscription.py"),
)


def _ruff_format(text: str, target: Path) -> str:
    """Format ``text`` as ruff would format ``target`` in this repository."""
    done = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--stdin-filename", str(target), "-"],
        input=text,
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO,
    )
    return done.stdout


@pytest.mark.parametrize(("async_path", "sync_path"), QUERY_PAIRS)
def test_the_committed_sync_twin_is_the_generated_one(async_path: str, sync_path: str) -> None:
    source = (SRC / async_path).read_text(encoding="utf-8")
    target = SRC / sync_path
    expected = _ruff_format(unasync.render(source, origin=f"src/niwaki/{async_path}"), target)
    assert target.read_text(encoding="utf-8") == expected, (
        f"{sync_path} is not the generated twin of {async_path} — edit the async "
        "source, then run `uv run python -m niwaki._codegen.unasync --write`"
    )


@pytest.mark.parametrize(("async_path", "sync_path"), QUERY_PAIRS)
def test_the_committed_sync_twin_carries_the_do_not_edit_header(
    async_path: str, sync_path: str
) -> None:
    first = (SRC / sync_path).read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith(f"# GENERATED from src/niwaki/{async_path} — DO NOT EDIT")


def test_the_check_mode_passes_on_the_repository() -> None:
    assert unasync.main(["--check"]) == 0


def test_the_query_pairs_are_generated() -> None:
    assert set(QUERY_PAIRS) <= set(unasync.PAIRS)


def test_every_generated_pair_stays_under_the_parity_guard() -> None:
    """A generated twin is still compared with its source, function by function."""
    guarded = {(pair.async_file, pair.sync_file) for pair in PARITY_PAIRS.values()}
    assert set(unasync.PAIRS) <= guarded


def test_the_subscription_socket_is_never_generated() -> None:
    """Threads versus tasks by design: the socket twins stay hand-written."""
    assert not [pair for pair in unasync.PAIRS if any("_subscription_socket" in p for p in pair)]


def test_the_generator_and_its_outputs_sit_outside_the_freshness_manifest() -> None:
    """Corpus-free, so never coupled to ``regen.sh`` (which needs the corpus)."""
    assert "unasync.py" not in {path.name for path in freshness._input_files()}
    artifacts = {path.resolve() for path in freshness._artifact_files()}
    for _, sync_path in unasync.PAIRS:
        assert (SRC / sync_path).resolve() not in artifacts


def test_the_generated_twins_stay_measured_by_coverage() -> None:
    config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    omit: list[str] = config["tool"]["coverage"]["run"].get("omit", [])
    for _, sync_path in unasync.PAIRS:
        path = str(SRC / sync_path)
        hits = [pattern for pattern in omit if fnmatch.fnmatch(path, pattern)]
        assert not hits, f"{sync_path} is omitted from coverage by {hits}"
