"""The corpus-free freshness guard — runs everywhere, including the public CI.

The rebuild-and-compare guards need the 1.7 GB schema corpus and skip without
it.  This one needs nothing: it recomputes the SHA-256 fingerprint of every
generator input and every generated artifact and compares against the manifest
written at the last regeneration.  A generator edited without a regen, an
artifact edited by hand, a new generator file not yet regenerated over — all
fail here, on any machine.
"""

from __future__ import annotations

from niwaki._codegen import freshness


def test_the_manifest_exists_and_everything_still_matches() -> None:
    problems = freshness.verify()
    assert not problems, "state diverged from the last regeneration:\n" + "\n".join(problems)


def test_the_inputs_cover_every_generator_source() -> None:
    """Glob-driven coverage: a new generator cannot dodge the manifest."""
    inputs = {path.name for path in freshness._input_files()}
    assert "generate.py" in inputs
    assert "generate_catalog.py" in inputs
    assert "freshness.py" in inputs  # this module guards itself
    assert "vocabulary.yaml" in inputs  # curation is an input like any other
    assert "naming.py" in inputs and "kinds.py" in inputs  # shared policy too


def test_the_artifacts_cover_every_generated_surface() -> None:
    names = {str(path) for path in freshness._artifact_files()}
    assert any("_generated" in name and name.endswith(".py") for name in names)
    assert any(name.endswith("_child_map.py") for name in names)
    assert any("_generated_cursors" in name for name in names)
    assert any(name.endswith("catalog.db") for name in names)
    # Emitted by regen and shipped with the public tests — a hand edit must
    # fail the guard, not parametrise the model suite from doctored fixtures.
    assert any(name.endswith("_test_data.json") for name in names)


def test_private_inputs_cover_the_extraction_pipeline_where_present() -> None:
    """data/scripts decides the Python type of every generated model."""
    import pytest

    names = {path.name for path in freshness._private_input_files()}
    if not names:
        pytest.skip("data/scripts (private extraction tooling) not present")
    assert "02_extract_props.py" in names


def test_absent_private_inputs_are_not_a_divergence(monkeypatch: object) -> None:
    """The public export omits data/ on purpose: recorded-but-absent private
    inputs must verify clean there — only files that exist are compared."""
    import pytest

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)
    mp.setattr(freshness, "_private_input_files", lambda: [])
    problems = [p for p in freshness.verify() if p.startswith("private_inputs")]
    assert problems == []


def test_a_divergence_is_reported_not_swallowed(tmp_path: object, monkeypatch: object) -> None:
    """Tamper detection: change one recorded hash, verify() must name it."""
    import json

    import pytest

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)
    recorded = json.loads(freshness.MANIFEST_PATH.read_text(encoding="utf-8"))
    victim = next(iter(recorded["artifacts"]))
    recorded["artifacts"][victim] = "0" * 64
    fake = tmp_path / "regen_manifest.json"  # type: ignore[operator]
    fake.write_text(json.dumps(recorded), encoding="utf-8")
    mp.setattr(freshness, "MANIFEST_PATH", fake)
    problems = freshness.verify()
    assert any(victim in problem for problem in problems)


def test_a_missing_manifest_fails_loud(monkeypatch: object, tmp_path: object) -> None:
    import pytest

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)
    mp.setattr(freshness, "MANIFEST_PATH", tmp_path / "absent.json")  # type: ignore[operator]
    problems = freshness.verify()
    assert problems and "missing" in problems[0]
