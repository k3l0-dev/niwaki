"""Executable documentation — every ``python`` fence in ``docs/`` runs.

How it works:

- Sybil collects each fenced ``python`` block of every Markdown page as a
  pytest item.  Blocks within one document share a namespace, so a page
  reads as one continuous program (a client opened in the first snippet is
  still connected in the last one).
- Every block runs with the :func:`snippet_apic` fixture active: an httpx
  mock whose callback is :meth:`niwaki.testing.FakeApic.handle` — the
  in-memory APIC the SDK ships.  POSTed design payloads are ingested into a
  DN-indexed store and reads answer from it, so describe → push → observe
  round-trips work exactly as printed, with zero visible scaffolding in the
  published snippet.
- ``<!--- skip: next --->`` immediately before a fence marks it
  non-executable (cobra comparisons, steps that need a live APIC).
  Shown-only output uses ``text`` / ``console`` fences.

Run the documentation suite alone with ``uv run pytest docs/``.
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock
from sybil import Sybil
from sybil.parsers.markdown import PythonCodeBlockParser, SkipParser

from niwaki.testing import FakeApic

_DOCUMENT_STORES: dict[str, FakeApic] = {}


@pytest.fixture
def snippet_apic(request: pytest.FixtureRequest, httpx_mock: HTTPXMock) -> FakeApic:
    """One stateful :class:`FakeApic` per documentation page.

    The store is keyed by document path, so every ``python`` fence of a page
    talks to the same fabric while the httpx interception stays scoped to
    the running block.

    Returns:
        The page's :class:`FakeApic` (exposed in the snippet namespace, but
        never referenced by published snippets).
    """
    fake = _DOCUMENT_STORES.setdefault(str(request.path), FakeApic())
    httpx_mock.add_callback(fake.handle, is_reusable=True, is_optional=True)
    return fake


SYBIL = Sybil(
    parsers=[PythonCodeBlockParser(), SkipParser()],
    patterns=["*.md", "**/*.md"],
    # reference/vocabulary is generated (tables, not tutorials): its fences are
    # for colouring, not execution — the generator is drift-guarded instead.
    excludes=["_build/**", "wiki/**", "reference/vocabulary/**"],
    fixtures=["snippet_apic"],
)

pytest_collect_file = SYBIL.pytest()
