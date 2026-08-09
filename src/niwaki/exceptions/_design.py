"""Design-DSL exception hierarchy.

All errors raised while building or pushing a design tree derive from
:class:`DesignError`, itself a :class:`~niwaki.exceptions.NiwakiError`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from niwaki.exceptions._base import NiwakiError

if TYPE_CHECKING:
    from niwaki.design._push import PushReport
    from niwaki.design._verify import RefCheck


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


class DanglingReferenceError(DesignError):
    """External references the APIC cannot honor, caught before the push.

    Raised by ``push(verify_refs=True)`` in ``strict``/``staged`` mode when
    at least one ``bind_dn``/literal-DN reference points at a DN the APIC
    does not serve (or serves with a class outside the referencing class's
    accept-set). Nothing has been written when this raises — verification
    is a read-only pass that runs before the first POST.

    Every failure is collected before raising (never first-fail); the
    message carries the full list with DNs in clear, and
    :attr:`failures` exposes the structured checks.

    Args:
        failures: One :class:`~niwaki.design.RefCheck` per failing
            reference, sorted by DN.

    Attributes:
        failures: The failing checks, as passed.
    """

    def __init__(self, failures: list[RefCheck]) -> None:
        self.failures = failures
        lines = []
        for check in failures:
            expected = ", ".join(check.expected) if check.expected else "any class"
            found = check.found or "nothing"
            detail = f" ({check.detail})" if check.detail else ""
            lines.append(
                f"  {check.ref.dn} [{check.status}] — expected {expected}, "
                f"found {found}{detail} (declared at {check.ref.declared_at})"
            )
        super().__init__(
            f"{len(failures)} external reference(s) cannot be honored by the APIC "
            "— nothing was pushed:\n" + "\n".join(lines)
        )


class StagedPushError(DesignError):
    """A ``push(mode="staged")`` partially succeeded.

    Carries the partial :class:`~niwaki.design.PushReport` (the DNs actually
    written, in the design's deterministic order — not the order the
    controller answered in) and the failures as plain
    ``(dn, exception)`` pairs — no engine internals leak into the public
    surface.

    Args:
        report: Partial push report — ``report.dns`` are the DNs written,
            including every independent branch that succeeded around a failure.
        failures: ``(dn, exception)`` for every operation that failed.
        not_run: DNs never attempted because an *ancestor* object failed —
            pushing them without their parent would only 404.  A failure
            isolates its own subtree; sibling branches are still written.

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


class DesignHintWarning(UserWarning):
    """A design the SDK can express but the fabric will not be happy with.

    Not an error: the push is legal, the APIC accepts it, and forbidding it
    would take away a shape somebody may genuinely want.  It is the case where
    the declaration is *provably* going to raise a fault — a floating SVI whose
    address is left at ``0.0.0.0`` lands outside its own subnet and the
    controller answers with a major fault every time.

    Its own category, so that ``warnings.simplefilter("error",
    DesignHintWarning)`` turns these into failures in a CI pipeline without
    touching every other ``UserWarning`` in the process, and
    ``simplefilter("ignore", DesignHintWarning)`` silences them without hiding
    anything else.
    """
