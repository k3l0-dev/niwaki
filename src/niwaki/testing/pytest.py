"""The opt-in pytest plugin of :mod:`niwaki.testing` — a fresh fake APIC per test.

Installing niwaki registers no pytest plugin: opt in from your root
``conftest.py``::

    pytest_plugins = ["niwaki.testing.pytest"]

Then request the ``fake_apic`` fixture by name.  This is the only module
of the SDK that imports ``pytest``.
"""

from __future__ import annotations

import pytest

from niwaki.testing import FakeApic

__all__ = ["fake_apic"]


@pytest.fixture
def fake_apic() -> FakeApic:
    """A fresh, empty :class:`~niwaki.testing.FakeApic` for each test.

    Returns:
        The fake — seed it, connect a client to it, inspect what it stored.

    Example::

        def test_push(fake_apic):
            with fake_apic.client() as aci:
                tenant("prod").bd("web").push(aci)
            assert fake_apic.get("uni/tn-prod/BD-web") is not None
    """
    return FakeApic()
