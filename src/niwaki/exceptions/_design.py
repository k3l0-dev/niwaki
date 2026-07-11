"""Design-DSL exception hierarchy.

All errors raised while building or pushing a design tree derive from
:class:`DesignError`, itself a :class:`~niwaki.exceptions.NiwakiError`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from niwaki.exceptions._base import NiwakiError

if TYPE_CHECKING:
    from niwaki.design._push import PushReport


class DesignError(NiwakiError):
    """Base class for all design-DSL errors."""


class UnknownMakerError(DesignError, AttributeError):
    """A maker name resolved at no level of the cursor's ancestor path.

    Also an :class:`AttributeError` so that ``hasattr`` and attribute
    protocols keep working on cursors.
    """


class DuplicateDeclarationError(DesignError):
    """The same object (class + naming) was declared twice in a design."""


class UnresolvedReferenceError(DesignError):
    """A ``bind``/``provide``/``consume`` target is not declared in the design.

    Raised during closed-world validation at push time.  The message includes
    the declared instances of the target class and a did-you-mean suggestion.
    """


class AmbiguousBindError(DesignError):
    """No unambiguous Rs class exists for a ``bind`` edge.

    Neither ``REFERENCE_MAP[owner][target]`` nor the inverse
    ``REFERENCE_MAP[target][owner]`` resolves to a relationship class.  Use
    ``.mo(RsClass, ...)`` to create the relationship explicitly.
    """


class StagedPushError(DesignError):
    """A ``push(mode="staged")`` partially succeeded.

    Carries the partial :class:`~niwaki.design.PushReport` (the DNs actually
    written, in execution order) and the failures as plain
    ``(dn, exception)`` pairs — no engine internals leak into the public
    surface.

    Args:
        report: Partial push report — ``report.dns`` are the DNs written
            before (and, in best-effort scenarios, around) the failure.
        failures: ``(dn, exception)`` for every operation that failed.
        not_run: DNs that were never attempted because an earlier wave failed.

    Example::

        from niwaki.exceptions import StagedPushError

        try:
            config.push(aci, mode="staged")
        except StagedPushError as exc:
            print(f"written : {exc.report.dns}")
            print(f"failed  : {[dn for dn, _ in exc.failures]}")
            print(f"skipped : {exc.not_run}")
    """

    def __init__(
        self,
        report: PushReport,
        failures: list[tuple[str, Exception]],
        not_run: list[str],
    ) -> None:
        self.report = report
        self.failures = failures
        self.not_run = not_run
        total = len(report.dns) + len(failures) + len(not_run)
        super().__init__(
            f"staged push failed: {len(failures)}/{total} operation(s) did not "
            f"succeed ({len(not_run)} never attempted)"
        )
