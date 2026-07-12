"""
Global pytest configuration for niwaki.

- Loads credentials from ``.env`` (if present) before any test session starts.
- Disables stamina delays (immediate retries) to speed up unit tests.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import stamina
from dotenv import load_dotenv
from pytest_httpx import HTTPXMock

from niwaki.facade import Niwaki

# Load .env from the repository root so APIC_* variables are available to
# both unit tests (env fallback) and integration tests.
load_dotenv(Path(__file__).parent.parent / ".env")

# ── Fixture loader ────────────────────────────────────────────────────────────

_FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    """
    Load a JSON fixture file from ``tests/fixtures/``.

    Args:
        name: Fixture filename without extension (e.g. ``"auth_login"``).

    Returns:
        Parsed fixture as a Python dict (deep-copied so callers can mutate it).

    Raises:
        FileNotFoundError: If the fixture file does not exist.
    """
    path = _FIXTURES_DIR / f"{name}.json"
    data: dict[str, Any] = json.loads(path.read_text())
    return copy.deepcopy(data)


# ── Stamina ───────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def disable_stamina_delays() -> None:
    """
    Enable stamina testing mode for all unit tests.

    In testing mode, stamina removes backoff delays between retry attempts
    (wait_initial, wait_max, wait_jitter are set to 0). Retries happen
    immediately, which avoids slowing down the test suite.

    Note:
        Integration tests marked ``integration`` may disable this fixture
        if they want to test real backoff behaviour.
    """
    stamina.set_testing(True)


# ── Shared APIC mock helpers ──────────────────────────────────────────────────

HOST = "https://apic.test"
LOGIN_URL = f"{HOST}/api/aaaLogin.json"


def login_payload(token: str = "tok", ttl: int = 600) -> dict[str, Any]:
    """aaaLogin response from the fixture with a controllable token and TTL."""
    data = load_fixture("auth_login")
    attrs: dict[str, Any] = data["imdata"][0]["aaaLogin"]["attributes"]
    attrs["token"] = token
    attrs["refreshTimeoutSeconds"] = str(ttl)
    return data


def ok() -> dict[str, Any]:
    """Empty successful APIC response."""
    return {"totalCount": "0", "imdata": []}


@pytest.fixture
def aci(httpx_mock: HTTPXMock) -> Niwaki:
    """Authenticated sync facade backed by pytest-httpx."""
    httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
    return Niwaki.connect(HOST, "admin", "secret")
