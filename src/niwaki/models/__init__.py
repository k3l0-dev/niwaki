"""Niwaki models package.

Public surface: the :class:`ManagedObject` base class and the ``REGISTRY``.

Importing generated models
--------------------------
Every ACI class is a generated model, addressable by its ACI package and
class name::

    from niwaki.models.fv.fvBD import fvBD
    from niwaki.models.enums.FvBDType import FvBDType

    import niwaki.models.fv as fv
    fv.BD  # short name, resolved lazily — the same class object

The canonical definitions live under ``niwaki.models._generated``; the public
``niwaki.models.<pkg>`` tree is a generated mirror of plain re-export modules
(``from niwaki.models._generated.fv.fvBD import fvBD as fvBD``).  Both paths
yield the very same class object, whose ``__module__`` stays canonical — so
``isinstance`` checks, pickles and the ``REGISTRY`` agree whichever path
imported it — and both resolve for static analysers (mypy, pyright, IDEs).
Importing a mirror package loads none of its classes; each loads on first use.
"""

from __future__ import annotations

from niwaki.models.base import REGISTRY, ManagedObject

__all__ = ["REGISTRY", "ManagedObject"]
