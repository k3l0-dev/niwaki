"""Niwaki utility modules.

Stateless helpers that operate on ACI data without requiring a live APIC
connection.  The two workhorses are re-exported here — the versioning policy
publishes ``niwaki.utils`` as a public path, so its advertised names must be
importable from it:

- :func:`niwaki.utils.diff.mo_diff` — surgical delta between two
  :class:`~niwaki.models.base.ManagedObject` instances (the comparator behind
  ``push(mode="plan")``).
- :func:`niwaki.utils.response.parse_imdata` — unwrap an APIC response
  envelope into typed objects.
"""

from __future__ import annotations

from niwaki.utils.diff import mo_diff
from niwaki.utils.response import parse_imdata

__all__ = ["mo_diff", "parse_imdata"]
