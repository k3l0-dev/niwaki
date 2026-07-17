# Plan before you push — the change-control gate

**Problem** — fabric changes should ship like code: proposed in a branch,
reviewed as a diff, applied on merge, idempotent on re-run.  Designs make this
natural — they *are* code, `plan` is the review artifact, `push` is the
deployment, and because pushes are upserts, re-running converges instead of
duplicating.  This recipe turns the `commerce` designs into a CI pipeline whose
gate is the plan.

## A design module is import-safe

Building a design performs no I/O, so CI can import every module just to plan
it.  One module per logical unit of intent, each exporting a `build()`:

```python
from niwaki.design import tenant


def build():
    config = tenant("commerce")
    config.vrf("prod")
    config.bd("bd-web", unicast_routing=True).bind(vrf="prod").subnet("10.30.10.1/24")
    return config
```

Nothing above touches the network — the module is safe to load in a linter, a
test, or a plan-only CI job.

## The runner

Credentials come from the environment — the CI runner's secret store, never the
repository.  The runner prints the plan as a diff, and only writes when asked:

```python
import os

from niwaki import Niwaki


def apply(config, *, plan_only: bool) -> bool:
    with Niwaki() as aci:                     # APIC_* environment variables
        plan = config.push(aci, mode="plan")
        for dn in plan.creates:
            print(f"+ {dn}")
        for dn, fields in plan.updates.items():
            for field, (current, desired) in fields.items():
                print(f"~ {dn} {field}: {current!r} -> {desired!r}")
        if plan_only or not plan.has_changes:
            return plan.has_changes
        config.push(aci)
        return plan.has_changes


# In CI these come from the runner's secret store; set here only so the page is
# self-contained and runnable.
os.environ["APIC_HOST"] = "https://apic.example.com"
os.environ["APIC_USERNAME"] = "admin"
os.environ["APIC_PASSWORD"] = "from-the-secret-store"

changed = apply(build(), plan_only=True)     # the merge-request job
assert changed is True                        # empty fabric: the tenant is new
```

The `+` / `~` lines are the merge-request comment: reviewers approve DNs and
field transitions, not screenshots.

## Apply on merge, and converge

The main-branch job drops `plan_only`.  Run it twice and watch idempotence: the
first apply writes, the second is a no-op because the design is converged:

```python
apply(build(), plan_only=False)               # main-branch job: writes
assert apply(build(), plan_only=False) is False   # re-run converges to a no-op
```

## The pipeline

Plan on every merge request, push on merge to main — the same two commands
whatever the CI system:

```yaml
plan:            # merge-request job
  script: python apply.py --plan

apply:           # main-branch job, behind a protected environment
  script: python apply.py
```

## Why this works

- **Eager failure** — reference typos and bad values fail the *plan* job, in the
  merge request, before anything reaches the fabric ({doc}`troubleshooting`).
- **Reviewable atomicity** — one design merges as one atomic `strict` POST: the
  fabric never holds half a merge request.
- **Idempotence** — a re-run after a flaky runner converges instead of
  duplicating; a green re-run is a no-op (`plan.has_changes is False`).

## Variations & pitfalls

- **Never echo secrets** — the plan output is configuration, not credentials;
  keep it that way by never printing the environment in CI logs
  ({doc}`../guide/connection`).
- **Many small designs** — one per tenant, one per rack; each plan then reads as
  one reviewable change, and the blast radius of any apply is one design.
- **Deletions stay explicit** — a design never prunes; removals are their own
  reviewed change (a script around `aci.node(...).delete()`), not a side effect
  of a merge.
- **Lab first** — point the same pipeline at a lab APIC through the environment,
  not the code: designs are environment-free by construction.
