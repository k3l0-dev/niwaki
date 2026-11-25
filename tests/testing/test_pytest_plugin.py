"""The opt-in pytest plugin ``niwaki.testing.pytest`` and its ``fake_apic`` fixture.

Contract pinned here:

- Loading is **opt-in** only: ``pytest_plugins = ["niwaki.testing.pytest"]``
  in the user's root ``conftest.py``.  The distribution registers no
  ``pytest11`` entry point, so installing niwaki never changes a test
  session's fixtures.
- ``fake_apic`` yields a fresh :class:`~niwaki.testing.FakeApic` per test.

The plugin is exercised in a child pytest session (``pytester``, run in a
subprocess so the child's imports never leak into this process).
"""

from __future__ import annotations

import importlib.metadata

import pytest

import niwaki.testing.pytest as plugin
from niwaki.testing import FakeApic

pytest_plugins = ["pytester"]

_USER_TESTS = """
from niwaki.testing import FakeApic

SEEN: list[FakeApic] = []


def test_first(fake_apic: FakeApic) -> None:
    assert isinstance(fake_apic, FakeApic)
    assert fake_apic.objects("fvTenant") == ()
    fake_apic.seed({"fvTenant": {"attributes": {"dn": "uni/tn-a", "name": "a"}}})
    SEEN.append(fake_apic)


def test_second_gets_a_fresh_fake(fake_apic: FakeApic) -> None:
    assert SEEN and fake_apic is not SEEN[0]
    assert fake_apic.objects("fvTenant") == ()


def test_the_fake_serves_a_client(fake_apic: FakeApic) -> None:
    with fake_apic.client() as aci:
        assert aci.query("fvTenant").fetch() == []
"""


def test_the_plugin_module_defines_the_fixture() -> None:
    assert callable(plugin.fake_apic)


def test_no_entry_point_auto_registers_the_plugin() -> None:
    registered = [
        ep
        for ep in importlib.metadata.entry_points(group="pytest11")
        if ep.value.startswith("niwaki")
    ]
    assert registered == []


def test_opting_in_provides_a_fresh_fake_per_test(pytester: pytest.Pytester) -> None:
    pytester.makeconftest('pytest_plugins = ["niwaki.testing.pytest"]\n')
    pytester.makepyfile(test_user=_USER_TESTS)
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=3)


def test_without_opting_in_there_is_no_fixture(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_user="def test_needs_it(fake_apic: object) -> None:\n    pass\n",
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*fixture 'fake_apic' not found*"])


def test_the_fixture_type_is_the_public_fake() -> None:
    assert FakeApic.__module__.startswith("niwaki.testing")
