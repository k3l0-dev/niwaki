"""Guards on the published versioning and deprecation policy.

``docs/project/versioning.md`` makes checkable promises: it enumerates the
public import paths, states that a deprecated name warns with its
replacement, and ``SECURITY.md`` advertises a supported release line.  Each
of those is a claim that can silently rot — a module renamed, a major
released, a warning re-hardcoded to a version number.  These tests fail when
the published policy stops matching the code.
"""

from __future__ import annotations

import importlib
import re
import warnings
from pathlib import Path

import pytest

import niwaki

ROOT = Path(__file__).parent.parent.parent
POLICY = ROOT / "docs" / "project" / "versioning.md"
SECURITY = ROOT / "SECURITY.md"
API_REFERENCE = ROOT / "docs" / "reference" / "api"

# Modules the reference documents through ``autoclass`` entries rather than a
# module-level ``automodule`` — deliberate, because their public surface is a
# handful of named classes rather than the whole namespace.  A new entry here
# is a decision, not an accident.
_CLASS_DOCUMENTED_ONLY = frozenset({"niwaki", "niwaki.models"})


def _published_public_paths() -> list[str]:
    """Import paths the policy page advertises as public.

    Returns:
        Every ``import niwaki...`` target found in the policy's Python block,
        in page order.
    """
    lines = POLICY.read_text(encoding="utf-8").splitlines()
    return [
        match.group(1)
        for line in lines
        if (match := re.match(r"^import (niwaki(?:\.\w+)*)", line.strip()))
    ]


def test_the_policy_page_lists_every_public_import_path() -> None:
    """The page is the enumeration of the public surface — it must be complete."""
    published = set(_published_public_paths())
    expected = {
        "niwaki",
        "niwaki.design",
        "niwaki.models",
        "niwaki.query",
        "niwaki.transport",
        "niwaki.exceptions",
        "niwaki.catalog",
    }
    assert published == expected, (
        "docs/project/versioning.md publishes the public surface; it drifted "
        f"from the expected set (missing: {expected - published}, "
        f"unexpected: {published - expected})"
    )


@pytest.mark.parametrize("path", _published_public_paths())
def test_every_published_path_actually_imports(path: str) -> None:
    """A path advertised as public that cannot be imported is a broken promise."""
    assert importlib.import_module(path) is not None


@pytest.mark.parametrize("path", _published_public_paths())
def test_every_published_path_is_documented_in_the_api_reference(path: str) -> None:
    """The policy and the API reference must agree on what is public.

    Asserts the Sphinx *directive*, not a substring: ``niwaki.utils`` appears
    inside ``automodule:: niwaki.utils.diff``, so a substring check would pass
    for a module the reference never documents.
    """
    text = "\n".join(p.read_text(encoding="utf-8") for p in sorted(API_REFERENCE.glob("*.md")))

    module_directive = re.search(rf"^\.\. automodule:: {re.escape(path)}$", text, re.MULTILINE)
    if module_directive is not None:
        return

    assert path in _CLASS_DOCUMENTED_ONLY, (
        f"{path} is published as public but the API reference has no "
        f"'automodule:: {path}' directive; document it, or add it to "
        "_CLASS_DOCUMENTED_ONLY with a reason"
    )
    class_directive = re.search(rf"^\.\. autoclass:: {re.escape(path)}\.", text, re.MULTILINE)
    assert class_directive is not None, f"{path} is documented nowhere in the API reference"


def test_security_policy_advertises_the_current_major() -> None:
    """A released major silently invalidates the supported-versions table.

    This went stale once already: the table advertised ``0.x`` while the SDK
    shipped 1.7.0.  It now fails the moment a new major ships.
    """
    major = niwaki.__version__.split(".")[0]
    text = SECURITY.read_text(encoding="utf-8")
    assert f"latest {major}.x release" in text, (
        f"SECURITY.md does not advertise the current major ({major}.x) as "
        "supported — update the table for this release line"
    )


def test_the_pinning_example_names_the_release_it_ships_with() -> None:
    """A page about pinning must not tell readers to pin the previous release.

    It shipped once naming ``1.7`` on the day ``1.8.0`` went out: copied
    verbatim, the ``==`` line excludes every fix in the release the reader is
    holding — including, that day, a catalogue concurrency fix the same page's
    changelog describes.  Both examples now move with the version.
    """
    version = niwaki.__version__
    minor = ".".join(version.split(".")[:2])
    text = (ROOT / "docs" / "project" / "versioning.md").read_text(encoding="utf-8")
    assert f"niwaki>={minor},<" in text, (
        f"the recommended floor pin does not name the current minor ({minor})"
    )
    assert f"niwaki=={version}" in text, (
        f"the reproducible-build pin does not name the shipped version ({version})"
    )


def test_deprecation_warning_states_the_rule_not_a_release_number() -> None:
    """The shim's message must not hardcode releases it cannot keep track of.

    A message naming fixed versions goes stale the moment the removal ships:
    it kept promising 1.5.0/1.7.0 after those releases were gone.  The rule
    ("no earlier than the next minor release") stays true forever, and the
    replacement name is what the caller actually needs.
    """
    import niwaki.domain._child_map as child_map
    from niwaki.design._cursor import _load_class
    from niwaki.facade import _navigate_jargon

    original = child_map.NAV_DEPRECATED.get("fvCtx")
    child_map.NAV_DEPRECATED["fvCtx"] = {"pim_ctx": "pimCtxP"}
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _navigate_jargon(_load_class("fvCtx"), "pim_ctx")
    finally:
        if original is None:
            del child_map.NAV_DEPRECATED["fvCtx"]
        else:
            child_map.NAV_DEPRECATED["fvCtx"] = original

    (warning,) = caught
    message = str(warning.message)
    assert "'pim'" in message, "the warning must name the replacement"
    assert not re.search(r"\d+\.\d+\.\d+", message), (
        f"the deprecation warning hardcodes a release number: {message!r} — "
        "state the rule instead, it cannot go stale"
    )
