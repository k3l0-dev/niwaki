"""Warnings attributed to the caller's line, not to a file inside the SDK.

A warning whose ``stacklevel`` lands on a library file is *hidden* by CPython's
default filters — the user never sees it, and the SDK looks silent about
something it took the trouble to detect.  ``skip_file_prefixes`` fixes that by
attributing the warning to the first frame outside this package, whatever the
call depth: the design DSL reaches a warning site through cursors, makers and
resolvers, and no fixed ``stacklevel`` is right for all of them.

Every warning the SDK raises goes through :func:`warn_at_caller`, so the
attribution rule lives in one place instead of being re-derived — with its own
frame count — at each site.
"""

from __future__ import annotations

import warnings
from pathlib import Path

_PKG_DIR = str(Path(__file__).resolve().parent)


def warn_at_caller(message: str, category: type[Warning]) -> None:
    """Emit *message* attributed to the first frame outside niwaki.

    Args:
        message: The warning text.  Say what is wrong and what to do about it —
            a warning the reader cannot act on is noise.
        category: The warning class, so a caller can silence or escalate this
            kind of warning without touching the others.

    Example::

        warn_at_caller(
            "floating SVI 'svi-1' has no floating_addr", DesignHintWarning
        )
    """
    warnings.warn(message, category, skip_file_prefixes=(_PKG_DIR,))
