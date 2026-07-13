# Testing your automation

Design-first pays off in tests: a design is a pure value, the network
boundary is plain httpx, and the session dependency is two small structural
protocols.  That gives your automation four natural test layers — from pure
unit tests to a fully faked APIC — none of which needs a fabric.

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

## An offline APIC at the HTTP boundary

The transport is plain httpx, so a fake APIC is just a mocked HTTP server —
[pytest-httpx](https://colin-b.github.io/pytest_httpx/) (or respx) is
enough.  Three routes cover a whole push test: login, the atomic POST, and
logout.  The assertion closes the loop: what went over the wire is exactly
`to_payload()`.

```python
# test_provisioning.py
import json

AUTH = {
    "imdata": [
        {
            "aaaLogin": {
                "attributes": {
                    "token": "fake-token",
                    "refreshTimeoutSeconds": "600",
                }
            }
        }
    ]
}


def test_shop_lands_in_one_post(httpx_mock):
    from niwaki import Niwaki

    httpx_mock.add_response(url="https://apic.test/api/aaaLogin.json", json=AUTH)
    httpx_mock.add_response(url="https://apic.test/api/mo/uni.json", json={"imdata": []})
    httpx_mock.add_response(url="https://apic.test/api/aaaLogout.json", json={"imdata": []})

    with Niwaki("https://apic.test", "admin", "test") as aci:
        build_shop("acme").push(aci)

    push = [r for r in httpx_mock.get_requests() if r.url.path == "/api/mo/uni.json"]
    assert len(push) == 1
    assert json.loads(push[0].content) == build_shop("acme").to_payload()
```

```console
$ uv run pytest test_provisioning.py
```

The same technique fakes reads: return an `{"imdata": [...]}` envelope of
`{"<class>": {"attributes": {...}}}` entries and the typed models parse it
exactly as they would a live answer.

## Stubbing the transport protocols

Code structured around a writer/reader dependency does not even need HTTP:
the push engine and the facade consume two structural protocols
({doc}`../reference/api/transport`), so any object with the right methods is
a valid transport:

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
