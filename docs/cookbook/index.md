# Cookbook

Task-oriented recipes for adopting the SDK, ordered the way a real deployment
grows.  They follow one fabric from its first tenant to confident day-2
operation — the `commerce` platform, a `prod` VRF, a `10.30.0.0/16` address
plan — so the recipes read as one continuous story, not twelve disconnected
snippets.

Every recipe takes one operator goal through the same arc — **describe** the
desired state as a design, **plan** it against the fabric, **push** it,
**verify** it through the read side.  Every code block on these pages is
executable and runs as a test in the SDK's own suite; copy any recipe and adapt
the names.

Recipes stay within the [curated vocabulary](../reference/vocabulary/index.md);
where a position is not curated, the recipe shows the honest escape hatch
(`.mo()`, `bind_dn()`, `static_path()`).

## Provision — grow the deployment

Start here and read in order: each recipe adds a layer to the same fabric.

```{toctree}
:maxdepth: 1

onboard-tenant
application-contracts
microsegmentation-esg
external-connectivity
turn-up-a-rack
```

## Operate — run it day to day

```{toctree}
:maxdepth: 1

day-2-changes
gitops-pipeline
fabric-audit
async-at-scale
```

## Diagnose — when something is wrong

```{toctree}
:maxdepth: 1

troubleshooting-connection
troubleshooting
```

## Migrate — coming from cobra

```{toctree}
:maxdepth: 1

migrate-from-cobra
```
