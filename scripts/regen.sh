#!/usr/bin/env bash
# Regenerate every generated artifact, in dependency order, from the schema corpus.
#
#   bash scripts/regen.sh                # full pipeline: extraction + codegen + format
#   bash scripts/regen.sh --from-subset  # skip extraction (data/extracted/ up to date)
#   bash scripts/regen.sh --verify       # regenerate, then run every drift/parity guard
#
# Pipeline (the one true order — each stage's inputs are its predecessors' outputs):
#
#   data/schemas/mo-apic-v6.0_9c/*.json        (corpus — 15,452 files, gitignored,
#   data/extracted/scopemeta_labels.json        backed up: scripts/backup_corpus.sh;
#                                               scopemeta is EXTERNAL — produced from
#                                               APIC ishell binaries, not regenerable)
#     │
#     ├─ 01_extract_classes.py  → data/extracted/classes.json
#     ├─ 02_extract_props.py    → data/extracted/properties.json
#     └─ 03_build_subset.py     → data/extracted/sdk_subset.json
#          │
#          ├─ generate_enums    → models/_generated/enums/ + enum_mapping.json
#          ├─ generate          → models/_generated/** + _PKG_MAP + tests/models/_test_data.json
#          │    ├─ generate_catalog → query/_catalog/catalog.db   (needs _PKG_MAP)
#          │    └─ generate_design  → design/_generated_cursors/  (also needs _child_map)
#          │         └─ generate_docs → docs/reference/vocabulary/ (last)
#          └─ generate_domain   → domain/_child_map.py  (schemas + vocabulary.yaml only)
#
# Raw generator output is NOT ruff-format-clean (all three Python trees) — the
# format pass at the end is part of the pipeline, not cosmetics.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

FROM_SUBSET=0
VERIFY=0
for arg in "$@"; do
    case "$arg" in
        --from-subset) FROM_SUBSET=1 ;;
        --verify) VERIFY=1 ;;
        *) echo "[regen] unknown argument: $arg" >&2; exit 2 ;;
    esac
done

if [ ! -d "data/schemas/mo-apic-v6.0_9c" ]; then
    echo "[regen] ERROR: schema corpus missing (data/schemas/mo-apic-v6.0_9c/)." >&2
    echo "[regen] Restore it from the private backup:" >&2
    echo "[regen]   git fetch origin corpus && git worktree add /tmp/corpus corpus" >&2
    echo "[regen]   tar -xf /tmp/corpus/corpus-*.tar.zst -C ." >&2
    exit 1
fi

if [ "$FROM_SUBSET" -eq 0 ]; then
    echo "[regen] extraction 1/3: classes..."
    uv run python data/scripts/01_extract_classes.py
    echo "[regen] extraction 2/3: properties..."
    uv run python data/scripts/02_extract_props.py
    echo "[regen] extraction 3/3: sdk subset..."
    uv run python data/scripts/03_build_subset.py
else
    [ -f "data/extracted/sdk_subset.json" ] || {
        echo "[regen] ERROR: --from-subset but data/extracted/sdk_subset.json is missing." >&2
        exit 1
    }
    echo "[regen] extraction skipped (--from-subset)"
fi

echo "[regen] codegen 1/6: enums..."
uv run python -m niwaki._codegen.generate_enums
echo "[regen] codegen 2/6: models..."
uv run python -m niwaki._codegen.generate
echo "[regen] codegen 3/6: domain tables..."
uv run python -m niwaki._codegen.generate_domain
echo "[regen] codegen 4/6: typed cursors..."
uv run python -m niwaki._codegen.generate_design
echo "[regen] codegen 5/6: read catalogue..."
uv run python -m niwaki._codegen.generate_catalog
echo "[regen] codegen 6/6: vocabulary book..."
uv run python -m niwaki._codegen.generate_docs

echo "[regen] format pass (raw generator output is not ruff-clean)..."
uv run ruff format --quiet \
    src/niwaki/models/_generated \
    src/niwaki/domain/_child_map.py \
    src/niwaki/design/_generated_cursors

echo "[regen] done — all artifacts regenerated."

if [ "$VERIFY" -eq 1 ]; then
    echo "[regen] verify: format/lint over everything..."
    uv run ruff format --check src tests docs scripts data/scripts
    uv run ruff check src tests docs scripts data/scripts
    echo "[regen] verify: drift/parity guards..."
    uv run pytest -q -p no:cacheprovider \
        tests/scripts tests/models tests/domain tests/design/test_generated_cursors.py \
        tests/design/test_core_yaml.py tests/design/test_docs_generator.py \
        tests/design/test_coverage_audit.py tests/query/test_catalog.py \
        tests/query/test_catalog_public.py
    echo "[regen] verify: vocabulary book is committed-identical..."
    git diff --exit-code -- docs/reference/vocabulary
    echo "[regen] verify: OK — every guard green."
fi

# The freshness manifest is written LAST, after --verify has passed (set -e
# aborts before this on any failure): it blesses the current state as the last
# good regeneration, so it must never record a state a guard just rejected.
echo "[regen] regen manifest (corpus-free freshness fingerprint)..."
uv run python -m niwaki._codegen.freshness --write
