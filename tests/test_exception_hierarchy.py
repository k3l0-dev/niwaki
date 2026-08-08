"""The published exception hierarchy must describe what actually ships.

``niwaki.exceptions.__doc__`` draws the whole hierarchy as an ASCII tree, and
that tree is the public error reference — it is rendered into the docs and read
straight out of the wheel by ``help(niwaki.exceptions)``.  A name in it that no
longer exists (or never did) sends a reader to write
``from niwaki.exceptions import ThatName`` and collect an ``ImportError``.

That happened: the tree advertised ``ApicVersionMismatchWarning``, a class from
an unbuilt milestone, and nothing failed.  These tests close the loop in both
directions — every drawn name resolves, and every exported name is drawn.
"""

from __future__ import annotations

import re

import niwaki.exceptions as exc

# The docstring names classes in two indented blocks: a ``from niwaki.exceptions
# import (...)`` listing, and the ASCII tree ("    ├── ForbiddenError   (why)").
# Both are indented; the surrounding prose is not — which is what tells them
# apart, so a sentence starting with a capital is never mistaken for a class.
_NAME = re.compile(r"^[ \t]+[│├└─\s]*([A-Z]\w+),?\s*(?:#|\(|$)")


def _drawn_names() -> set[str]:
    """Every class name the module docstring presents as importable."""
    doc = exc.__doc__ or ""
    return {match.group(1) for line in doc.splitlines() if (match := _NAME.match(line))}


def test_the_docstring_actually_draws_a_hierarchy() -> None:
    """Guard the guard: a parser that silently matches nothing proves nothing."""
    names = _drawn_names()
    assert len(names) > 15, f"only {len(names)} names parsed — the tree format changed"
    assert "APIError" in names
    assert "DesignHintWarning" in names


def test_every_drawn_name_is_importable() -> None:
    """No phantom entries: what the tree promises, the package delivers."""
    builtins = {"Exception", "UserWarning"}
    missing = sorted(n for n in _drawn_names() - builtins if not hasattr(exc, n))
    assert not missing, (
        f"the published hierarchy names {missing}, which niwaki.exceptions does not "
        "export — remove them from the docstring or implement them"
    )


def test_every_exported_exception_is_drawn() -> None:
    """The other direction: a shipped error the reference never mentions."""
    exported = {
        name
        for name in exc.__all__
        if isinstance(getattr(exc, name, None), type)
        and issubclass(getattr(exc, name), BaseException)
    }
    undrawn = sorted(exported - _drawn_names())
    assert not undrawn, f"exported but absent from the published hierarchy: {undrawn}"
