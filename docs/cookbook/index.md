# Cookbook

Task-oriented recipes: each one takes a real operator problem and walks it
through the same arc — **describe** the desired configuration as a design,
**plan** it against the fabric, **push** it, **verify** it through the read
side.  Every code block on these pages is executable and runs as a test in
the SDK's own suite; copy any recipe and adapt the names.

All recipes stay within the [curated vocabulary](../reference/vocabulary/index.md)
unless explicitly flagged — where a position is not curated yet, the recipe
shows the honest escape hatch (`.mo()`, `bind_dn()`) and links the
*vocabulary request* issue template.

## Provision

```{toctree}
:maxdepth: 1

three-tier-app
access-policies-vpc
static-paths
l3out-basic
```

## Fabric day-0

```{toctree}
:maxdepth: 1

fabric-baseline
node-registration
```

## Day-2

```{toctree}
:maxdepth: 1

vlan-migration
```

## Operate

```{toctree}
:maxdepth: 1

fabric-audit
troubleshooting
```

## Integrate

```{toctree}
:maxdepth: 1

gitops-pipeline
```

## Migrate

```{toctree}
:maxdepth: 1

migrate-from-cobra
```
