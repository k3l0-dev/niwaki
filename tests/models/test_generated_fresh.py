"""Freshness guard — committed generated models match a fresh render (sampled).

The v1.3.2 drift (catalogue missing eleven shipped classes) lived three
releases because nothing compared the generated tree against its inputs.
Re-rendering all 2,222 classes per test run is too slow; a deterministic
sample across the naming-sensitive families (l3ext digit-split, vmm/vns
collision scope, scopemeta-labelled, enum-heavy) catches a generator or
input change without a regen. Corpus- and subset-gated.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from niwaki._codegen import generate as gen

READY = gen.SUBSET_FILE.exists() and gen.MAPPING_FILE.exists()
needs_subset = pytest.mark.skipif(
    not READY, reason="data/extracted subset not present (run data/scripts 01-03)"
)

# Deliberate spread: the digit-split family whose names are frozen in the
# catalogue, the collision-scope divergent classes, everyday heavy hitters,
# scopemeta-labelled AAA, and enum-rich policies.
SAMPLE = [
    "fvTenant",
    "fvBD",
    "fvAEPg",
    "l3extOut",
    "l3extMember",
    "l3extRsPathL3OutAtt",
    "vmmAgtStatus",
    "vnsCMgmt",
    "aaaAuthRealm",
    "bgpPeerP",
    "spanVDest",
    "telemetryFteEventTcpFlags",
    "infraAttEntityP",
    "maintCatMaintP",
    "datetimeNtpAuthKey",
]


@needs_subset
def test_committed_models_match_regeneration_sample() -> None:
    from jinja2 import Environment, FileSystemLoader, StrictUndefined

    subset = json.loads(gen.SUBSET_FILE.read_text())
    inputs = gen._CodegenInputs(
        enum_mapping=json.loads(gen.MAPPING_FILE.read_text()),
        sm_labels=json.loads(gen.SM_LABELS_FILE.read_text()) if gen.SM_LABELS_FILE.exists() else {},
    )
    env = Environment(
        loader=FileSystemLoader(str(gen.TEMPLATES_DIR)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    ruff = shutil.which("ruff")
    assert ruff, "ruff not on PATH (dev environment)"

    stale: list[str] = []
    for aci_class in SAMPLE:
        assert aci_class in subset, f"{aci_class} missing from sdk_subset.json"
        rendered = gen._render_class(aci_class, subset[aci_class], env, inputs)
        # The committed tree is ruff-formatted by regen.sh — normalise before
        # comparing so the guard tests content, not formatting drift.
        formatted = subprocess.run(
            [ruff, "format", "-"], input=rendered, capture_output=True, text=True, check=True
        ).stdout
        pkg = subset[aci_class]["class"]["class_pkg"]
        committed = (gen.OUTPUT_DIR / pkg / f"{aci_class}.py").read_text()
        if formatted != committed:
            stale.append(aci_class)

    assert not stale, (
        f"generated models diverge from a fresh render: {stale} — "
        "regenerate with bash scripts/regen.sh --from-subset"
    )
