"""Niwaki models package.

Public surface: ManagedObject base class and the REGISTRY.
Generated primitives live in models._generated and are imported there.

Import ergonomics
-----------------
Generated models are addressable with or without the ``_generated`` segment::

    # Canonical (always works)
    from niwaki.models._generated.fv.fvBD import fvBD

    # Short alias (same object, no copy)
    from niwaki.models.fv.fvBD import fvBD

The alias is implemented by :class:`_GeneratedAlias`, a ``MetaPathFinder``
installed in ``sys.meta_path`` below.  It intercepts ``niwaki.models.<pkg>.*``
imports, delegates to ``niwaki.models._generated.<pkg>.*``, and registers the
result under both names in ``sys.modules`` so only one class object ever exists.
"""

from __future__ import annotations

import importlib
import sys
import types
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec

from niwaki.models.base import REGISTRY, ManagedObject


class _AliasLoader(Loader):
    """Trivial loader that returns an already-imported module without re-executing it."""

    def __init__(self, module: types.ModuleType) -> None:
        self._module = module

    def create_module(self, spec: ModuleSpec) -> types.ModuleType | None:
        return self._module

    def exec_module(self, module: types.ModuleType) -> None:
        pass


class _GeneratedAlias(MetaPathFinder):
    """Redirect ``niwaki.models.<pkg>.*`` → ``niwaki.models._generated.<pkg>.*``.

    Only intercepts names that do **not** already start with
    ``niwaki.models._generated``, so canonical paths are never touched.

    The alias and canonical name share the same object in ``sys.modules`` —
    no duplicate class definitions, REGISTRY stays consistent, ``isinstance``
    checks work correctly across both import styles.

    Performance cost:
    - One ``list.insert`` at startup (nanoseconds).
    - Per-import: a single ``str.startswith`` check — returns ``None`` for
      canonical imports.  First alias import costs the same as the direct import
      (one module file read); all subsequent accesses hit ``sys.modules`` directly.
    """

    _PREFIX = "niwaki.models."
    _SKIP = "niwaki.models._generated"

    def find_spec(
        self,
        fullname: str,
        path: object,
        target: object = None,
    ) -> ModuleSpec | None:
        """Return a spec aliasing *fullname* to its ``_generated`` counterpart.

        Args:
            fullname: Fully-qualified module name being imported.
            path:     Parent package path (unused — we derive from *fullname*).
            target:   Existing module when reloading (unused).

        Returns:
            A :class:`~importlib.machinery.ModuleSpec` backed by
            :class:`_AliasLoader`, or ``None`` if *fullname* is not a
            ``niwaki.models.*`` alias target.
        """
        if not fullname.startswith(self._PREFIX):
            return None
        if fullname.startswith(self._SKIP):
            return None

        real_name = "niwaki.models._generated." + fullname[len(self._PREFIX) :]

        try:
            real_mod = importlib.import_module(real_name)
        except ImportError:
            return None

        loader = _AliasLoader(real_mod)
        spec = ModuleSpec(fullname, loader, origin=getattr(real_mod, "__file__", None))
        if hasattr(real_mod, "__path__"):
            spec.submodule_search_locations = list(real_mod.__path__)
        return spec


sys.meta_path.insert(0, _GeneratedAlias())

__all__ = ["REGISTRY", "ManagedObject"]
