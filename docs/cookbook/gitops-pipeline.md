# Designs in CI — a GitOps pipeline

**Problem** — fabric changes should ship like code: proposed in a branch,
reviewed as a diff, applied on merge.  Designs make this natural — they
*are* code, `plan` is the review artifact, `push` is the deployment, and
upserts make re-runs idempotent.

## The repository shape

One Python module per logical unit of intent, exporting a design:

```text
fabric-config/
├── designs/
│   ├── shop_tenant.py        # the three-tier app
│   ├── rack12_access.py      # access policies + vPC
│   └── fabric_baseline.py    # NTP/DNS/syslog/BGP
├── apply.py                  # the tiny runner below
└── ci.yml                    # your CI system's pipeline definition
```

A design module is import-safe by construction — building a design performs
no I/O, so CI can load every module just to *plan* it:

```python
from niwaki.design import tenant


def build():
    config = tenant("shop")
    config.vrf("prod")
    config.bd("web", unicast_routing=True).bind(vrf="prod")
    return config
```

## The runner

Credentials come from the environment (the CI runner's secret store —
never the repository); `--plan` gates, push applies:

```python
import sys

from niwaki import Niwaki


def apply(design, plan_only: bool) -> int:
    with Niwaki() as aci:                     # APIC_* env vars
        plan = design.push(aci, mode="plan")
        for dn in plan.creates:
            print(f"+ {dn}")
        for dn, fields in plan.updates.items():
            for field, (current, desired) in fields.items():
                print(f"~ {dn} {field}: {current!r} -> {desired!r}")
        if plan_only or not plan.has_changes:
            return 0
        design.push(aci)
        return 0


import os

os.environ.setdefault("APIC_HOST", "https://apic.example.com")
os.environ.setdefault("APIC_USERNAME", "admin")
os.environ.setdefault("APIC_PASSWORD", "secret")

exit_code = apply(build(), plan_only="--plan" in sys.argv)
assert exit_code == 0
```

The `+` / `~` lines are the merge-request comment: reviewers approve DNs
and field transitions, not screenshots.

## The pipeline

Plan on every merge request, push on merge to main — the same two commands
whatever the CI system:

```yaml
# merge request / pull request job
plan:
  script: python apply.py --plan

# main-branch job, behind your protected environment
apply:
  script: python apply.py
```

## Why this works

- **Idempotence** — pushes are upserts: re-running the apply job after a
  flaky runner converges instead of duplicating.  A green re-run is a
  no-op (`plan.has_changes is False`).
- **Reviewable atomicity** — one design merges as one atomic `strict`
  POST: the fabric never holds half a merge request.
- **Eager failure** — reference typos fail the *plan* job, in the merge
  request, before anything can reach the fabric
  ({doc}`troubleshooting`).

## Variations & pitfalls

- **Never print secrets** — the plan output contains configuration, not
  credentials; keep it that way by never echoing the environment in CI
  logs ({doc}`../guide/connection`).
- **Scope per design** — plan reads pull each declared domain's subtree;
  many small designs (one per tenant, one per rack) keep plans fast and
  reviews focused ({doc}`../guide/push-modes`).
- **Deletions are explicit** — a design never prunes; removals are their
  own reviewed change (a script around `aci.node(...).delete()`), not a
  side effect of a merge.
- **Lab first** — point the same pipeline at a lab APIC via environment,
  not code: the designs are environment-free by construction.
