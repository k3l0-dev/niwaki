"""Integration fixtures — a live APIC connection from the environment.

Credentials come from ``APIC_HOST`` / ``APIC_USERNAME`` / ``APIC_PASSWORD``
(loaded from ``.env`` at the repo root by ``tests/conftest.py``).  When they are
absent, or the APIC is unreachable, the integration suite is skipped rather than
failing — so the unit suite stays green on any machine.
"""

from __future__ import annotations

import os
from collections.abc import Generator

import pytest

from niwaki import Niwaki
from niwaki.exceptions import TransportError


@pytest.fixture(scope="session")
def live_aci() -> Generator[Niwaki, None, None]:
    """Authenticated :class:`~niwaki.Niwaki` façade against the lab APIC.

    Session-scoped: one login shared across the integration suite, closed at the
    end of the session.
    """
    missing = [v for v in ("APIC_HOST", "APIC_USERNAME", "APIC_PASSWORD") if not os.getenv(v)]
    if missing:
        pytest.skip(f"{', '.join(missing)} not set — integration needs a live APIC (.env)")

    try:
        aci = Niwaki.connect(
            os.environ["APIC_HOST"],
            os.environ["APIC_USERNAME"],
            os.environ["APIC_PASSWORD"],
            verify_ssl=False,  # lab simulators use self-signed certificates
        )
    except TransportError:
        pytest.skip("APIC unreachable — integration suite skipped")

    yield aci  # type: ignore[misc]
    aci.close()
