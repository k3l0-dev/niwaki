# Testing your automation

Design-first pays off in tests: a design is a pure value and the network
boundary is plain httpx, so the natural place to fake the APIC is the HTTP
layer — and niwaki ships that fake, the one its own documentation runs on.
That gives your automation natural test layers — from pure unit tests to a
fully faked APIC — none of which needs a fabric.

## Designs are pure — assert on the payload

A design factory is an ordinary function returning an ordinary value.
`to_payload()` runs validation and reference resolution and returns the
exact wire payload — no client, no network:

```python
from niwaki.design import tenant


def build_shop(tenant_name):
    config = tenant(tenant_name)
    config.vrf("prod")
    config.bd("web", unicast_routing=True).bind(vrf="prod")
    return config


payload = build_shop("acme").to_payload()

tn = payload["polUni"]["children"][0]["fvTenant"]
assert tn["attributes"]["name"] == "acme"
assert {next(iter(child)) for child in tn["children"]} == {"fvBD", "fvCtx"}
```

Because `to_payload()` resolves every reference closed-world, a single call
in a unit test catches a `bind()` typo before any fabric exists:

```python
import pytest

from niwaki.exceptions import UnresolvedReferenceError

typo = tenant("acme")
typo.vrf("prod")
typo.bd("web").bind(vrf="prdo")  # no such declaration

with pytest.raises(UnresolvedReferenceError):
    typo.to_payload()
```

(`UnresolvedReferenceError` is a {class}`~niwaki.exceptions.DesignError` —
catch the parent when you only care that the design is invalid.)

## The plan is a test

Against a lab fabric, `plan` turns convergence into an assertion: push, then
require an empty diff.  This is the strongest integration test a design can
have — every declared attribute must round-trip.

```python
from niwaki import Niwaki

aci = Niwaki.connect("https://apic.example.com", "admin", "secret")
```

The `with` form closes the session for you; `connect()` is used here so the
rest of the page can share one client — see {doc}`connection`.

```python
config = build_shop("acme")
config.push(aci)

assert config.push(aci, mode="plan").has_changes is False
```

## A fake APIC in your tests

`niwaki.testing` ships {class}`~niwaki.testing.FakeApic`: an in-memory APIC
that answers the HTTP a client sends — login, pushes, deletes, reads with
the full query grammar — from a store of objects indexed by DN.  Your code
runs unchanged against it; the fake is the network.

```python
from niwaki.testing import FakeApic

fake = FakeApic()
with fake.client() as offline:
    build_shop("acme").push(offline)
    bd = offline.tenant("acme").bd("web").read()

assert bd.unicast_routing is True
assert fake.get("uni/tn-acme/ctx-prod") is not None
```

`fake.client()` returns a {class}`~niwaki.Niwaki` wired to the fake
(`fake.async_client()` an {class}`~niwaki.AsyncNiwaki`); any keyword is
relayed to the constructor.  For a client you build yourself, the fake is
also an `httpx` transport (`fake.transport`), and `fake.handle` is a plain
request → response callback for pytest-httpx or respx.  What the fake
holds is read back with `fake.get(dn)` and `fake.objects(aci_class)` —
read-only {class}`~niwaki.testing.StoredObject` values.

### With pytest

The fixture is an opt-in plugin — installing niwaki registers nothing.
Opt in from your root `conftest.py`:

```python
# conftest.py
pytest_plugins = ["niwaki.testing.pytest"]
```

Every test that names `fake_apic` gets a fresh, empty fake.  The request
log closes the loop: what went over the wire is exactly `to_payload()`.

```python
# test_provisioning.py
import json


def test_shop_lands_in_one_post(fake_apic):
    with fake_apic.client() as aci:
        build_shop("acme").push(aci)

    (push,) = [r for r in fake_apic.requests if r.url.path == "/api/mo/uni.json"]
    assert json.loads(push.content) == build_shop("acme").to_payload()
    assert [o.dn for o in fake_apic.objects("fvBD")] == ["uni/tn-acme/BD-web"]


test_shop_lands_in_one_post(FakeApic())  # what pytest does for you
```

### Reads answer like a controller

Seed the store with an APIC envelope — the shape a POST carries, at any
depth — then read it back.  Filters are **evaluated** the way the fabric
evaluates them: a boolean whatever its spelling, a number numerically, a
bitmask as a set, a named number by value.

```python
fake = FakeApic()
fake.seed(
    {
        "fvTenant": {
            "attributes": {"dn": "uni/tn-acme", "name": "acme"},
            "children": [
                {"fvBD": {"attributes": {"name": "web", "arpFlood": "yes"}}},
                {"fvBD": {"attributes": {"name": "db", "arpFlood": "no"}}},
            ],
        }
    }
)

with fake.client() as offline:
    flooded = offline.query("fvBD").where(arpFlood=True).fetch()
    assert offline.query("fvBD").under("uni/tn-acme").count() == 2

assert [bd.dn for bd in flooded] == ["uni/tn-acme/BD-web"]
```

It also stores what it is given the way an APIC does, so a test cannot pass
on a parroted payload: a bitmask comes back in another order than written,
and a number the schema names is stored by its name.

```python
with fake.client() as offline:
    tenant("acme").filter("web").entry("http", tcp=80).push(offline)

entry = fake.get("uni/tn-acme/flt-web/e-http")
assert entry is not None
assert entry.attributes["dFromPort"] == "http"
```

### A lab capture becomes a fixture

{func}`niwaki.snapshot.take` captures a scope of a real fabric;
{meth}`FakeApic.from_snapshot <niwaki.testing.FakeApic.from_snapshot>`
rebuilds it offline, object for object — a capture of the replayed fake is
the same bytes.  Plan your next change against yesterday's fabric, with no
fabric:

```python
from niwaki import snapshot

captured = snapshot.take(aci, "uni/tn-acme")  # the lab client from above
replica = FakeApic.from_snapshot(captured)

with replica.client() as offline:
    assert build_shop("acme").push(offline, mode="plan").has_changes is False
```

### Failures on demand

`fake.fail(dn, ...)` makes the next requests that target a DN answer an
APIC error, which the SDK raises as its typed exception — `apic_code`
included.  A failed write changes nothing: an APIC POST is atomic.

```python
from niwaki.exceptions import APIError

fake = FakeApic()
fake.fail("uni/tn-acme/BD-web", code="103", text="BD quota exceeded")

with fake.client() as offline, pytest.raises(APIError) as caught:
    build_shop("acme").push(offline)

assert caught.value.apic_code == "103"
assert [o.dn for o in fake.objects()] == ["uni"]  # only the universe, as before
```

Narrow a failure to one verb (`method="POST"`) or a number of requests
(`times=2`); `fake.clear_failures()` drops them all.

### It fails loud, never wrong

A request the fake cannot answer faithfully is refused with an APIC error
whose message starts with `"FakeApic:"` (from `seed()`, a `ValueError` with
that message).  That covers an unknown parameter, class or property, a
filter operator outside the APIC grammar, a response facet it has no data
for — and everything it would otherwise have to guess: ordering an enum,
an address or a bitmask; a wildcard the APIC rejects (a leading `*`, any
wildcard on an IP, a `dn` or a `descr`); a filter term qualified with a
class the query does not return, or met by an object of another class; a
response-subtree parameter with nothing embedded; a placement that
contradicts itself (a body `dn` that is not the URL's, or whose RN its
naming properties do not compute).  A test that reaches it learns that the
fake, not the code, is the limit.

```python
from niwaki.query import SubtreeInclude

with FakeApic().client() as offline, pytest.raises(APIError) as caught:
    offline.query("fvBD").include_subtree(SubtreeInclude.AUDIT_LOGS).fetch()

assert caught.value.apic_message.startswith("FakeApic:")
```

### What the fake is not

- **No RBAC.**  Every login succeeds; a write your user may not perform is
  stored anyway.
- **No WebSocket subscriptions.**  A subscription request is refused.
- **No policy validation, no containment check.**  A design the controller
  would reject — a value out of the fabric's policy, an object under a parent
  that does not exist or cannot contain it — is stored.  Convergence against
  a lab fabric (`plan`, above) stays the test of that.
- **No fault is ever raised.**  A fault exists when you seed a `faultInst`
  under an object; a failure when you inject it with `fail()`.
- **One implicit object only.**  Like every controller, a fake always holds
  the policy universe — a `polUni` at `uni`, which cannot be deleted.
  Everything else is what was written or seeded, and reads return the
  attributes that were stored — not the defaults a controller would add
  (filters do compare against the schema defaults).

## The write seam, as a checkable contract

What the push engine asks of a session is documented by small structural
protocols ({doc}`../reference/api/transport`): ``post_mo`` and
``delete_mo`` for writes, ``get_mo`` for typed reads.  If you build your
own fake for engine-level tests, you can *prove* it has the right shape —
structurally, no inheritance needed:

```python
class RecordingWriter:
    """Collects every (dn, payload) the engine would send."""

    def __init__(self):
        self.posted = []

    def post_mo(self, dn, payload):
        self.posted.append((dn, payload))

    def delete_mo(self, dn):
        pass


from niwaki.transport._protocols import MoWriter

assert isinstance(RecordingWriter(), MoWriter)  # structural — no inheritance
```

## What not to test

The SDK's own behaviour — validation, reference resolution, wire naming,
push semantics — ships with its own test suite.  Test **your intent**: the
designs your code builds (payload shape), the decisions it takes (which
design, which mode), and its convergence on a lab fabric (plan).

## Next steps

- {doc}`../cookbook/gitops-pipeline` — plan as a CI gate
- {doc}`errors` — asserting on failure behaviour
