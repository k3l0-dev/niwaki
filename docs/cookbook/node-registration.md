# Registering fabric nodes

**Problem** — new switches sit in the fabric membership queue with a serial
number and no identity.  Registration is a `uni/controller` policy: map each
serial to a node id, a name and a role — declaratively, like everything
else.

## The design

```python
from niwaki import Niwaki
from niwaki.design import controller

inventory = [
    ("FDO12345ABC", "101", "leaf-101", "leaf"),
    ("FDO12345ABD", "102", "leaf-102", "leaf"),
    ("FDO54321XYZ", "201", "spine-201", "spine"),
]

config = controller()
members = config.fabric_membership()
for serial, node_id, name, role in inventory:
    members.fabric_node_member(serial, id=node_id, name=name, role=role)
```

The serial is the naming property (`nodep-{serial}`): re-pushing the same
inventory is an upsert, and a *changed* name for the same serial is exactly
the kind of drift `plan` will show.

## Plan, push, verify

```python
aci = Niwaki.connect("https://apic.example.com", "admin", "secret")

plan = config.push(aci, mode="plan")
print(f"registering {len(plan.creates) - 2} nodes")   # minus the two parents

config.push(aci)

members = aci.query("fabricNodeIdentP").fetch()
assert {m.name for m in members} == {"leaf-101", "leaf-102", "spine-201"}
```

The `with` form closes the session for you; `connect()` is used here so the
rest of the page can share one client — see {doc}`../guide/connection`.

Once the fabric has absorbed the registration, the operational view catches
up — `aci.query("topSystem").fetch()` lists the nodes with their roles and
addresses ({doc}`fabric-audit`).

## Variations & pitfalls

- **Node ids are forever** — ACI will not renumber a registered node; get
  the id scheme right in the inventory (odd/even leaf pairs, spines in
  their own block) before the first push.
- **The source of truth is yours** — `inventory` above is a list literal,
  but nothing stops it being a CSV, an IPAM export or a CMDB query; the
  design is plain Python ({doc}`gitops-pipeline`).
- **Discovery is operational** — the membership *policy* is declarative;
  watching nodes come up (`topSystem`, `fabricNode`) is a read loop, which
  is the facade's job, not a design's.
