"""Test your automation without a fabric — an in-memory APIC that speaks the SDK's HTTP.

:class:`FakeApic` answers the requests a :class:`~niwaki.Niwaki` client sends —
login, design pushes, deletes, reads with the full query grammar — from a
DN-indexed store, and refuses loudly whatever it cannot answer faithfully.
:class:`StoredObject` is the read-only view of what it holds.

Nothing here is imported by ``import niwaki``; import ``niwaki.testing`` from
your tests.  A pytest fixture is available as an opt-in plugin::

    # conftest.py
    pytest_plugins = ["niwaki.testing.pytest"]

    # test_provisioning.py
    def test_push(fake_apic):
        with fake_apic.client() as aci:
            build_shop("acme").push(aci)
        assert fake_apic.get("uni/tn-acme") is not None

Example::

    from niwaki.design import tenant
    from niwaki.testing import FakeApic

    fake = FakeApic()
    with fake.client() as aci:
        tenant("prod").bd("web", unicast_routing=True).push(aci)
        assert aci.tenant("prod").bd("web").read().unicast_routing is True
"""

from __future__ import annotations

from niwaki.testing._fake_apic import FakeApic
from niwaki.testing._store import StoredObject

__all__ = ["FakeApic", "StoredObject"]
