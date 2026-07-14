"""Niwaki — a modern, typed Python SDK for Cisco ACI (APIC).

One mental model: the **design DSL describes** the desired configuration,
``push()`` **applies** it, and the **facade observes** (vocabulary
navigation, typed reads, queries, deletion).

Public entry points, importable straight from the package root:

- :class:`~niwaki.Niwaki` / :class:`~niwaki.AsyncNiwaki` — connected clients
  (and the :class:`~niwaki.NiwakiNode` / :class:`~niwaki.AsyncNiwakiNode`
  handles they hand out).
- :func:`~niwaki.design.design` / :func:`~niwaki.design.tenant` /
  :func:`~niwaki.design.infra` / :func:`~niwaki.design.fabric` /
  :func:`~niwaki.design.controller` — roots of the design DSL (imported
  lazily; canonical home is :mod:`niwaki.design`).
- :class:`~niwaki.transport.RetryConfig` — transport retry policy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from niwaki.facade import AsyncNiwaki, AsyncNiwakiNode, Niwaki, NiwakiNode
from niwaki.transport._config import RetryConfig

if TYPE_CHECKING:
    from niwaki.design import controller as controller
    from niwaki.design import design as design
    from niwaki.design import fabric as fabric
    from niwaki.design import infra as infra
    from niwaki.design import tenant as tenant

__version__ = "0.8.0"
__all__ = [
    "AsyncNiwaki",
    "AsyncNiwakiNode",
    "Niwaki",
    "NiwakiNode",
    "RetryConfig",
    "controller",
    "design",
    "fabric",
    "infra",
    "tenant",
]

_DESIGN_ROOTS = frozenset({"controller", "design", "fabric", "infra", "tenant"})


def __getattr__(name: str) -> Any:
    """Lazily expose the design DSL roots without paying their import cost.

    ``from niwaki import tenant`` (or ``design``, ``infra``, ``fabric``,
    ``controller``) works, but the design package (and its generated typed
    cursors) is only imported on first access, keeping the ``import niwaki``
    cold-start budget intact.
    """
    if name in _DESIGN_ROOTS:
        import niwaki.design

        return getattr(niwaki.design, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
