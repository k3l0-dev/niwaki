"""Root exception for the niwaki SDK."""

from __future__ import annotations


class NiwakiError(Exception):
    """Base class for all niwaki SDK errors."""


class MissingDependencyError(NiwakiError):
    """An optional feature was used without the extra that provides it.

    Raised at import or first use rather than at the first request, so the
    message names the extra to install instead of surfacing as a confusing
    failure deep in the transport.

    Example::

        pip install niwaki[x509]
    """
