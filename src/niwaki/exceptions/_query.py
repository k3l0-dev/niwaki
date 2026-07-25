"""Query execution errors.

Raised by the single-object executor :meth:`~niwaki.query.Query.one`, which
demands exactly one match.  Both are direct children of
:class:`~niwaki.exceptions.NiwakiError` — the precedent is Django's
``DoesNotExist`` / ``MultipleObjectsReturned`` — so a caller can target the
outcome precisely rather than reaching for the HTTP-flavoured
:class:`~niwaki.exceptions.NotFoundError` (which carries a status code and a
different meaning).
"""

from __future__ import annotations

from niwaki.exceptions._base import NiwakiError


class NoResultError(NiwakiError):
    """A query that required exactly one object matched none.

    Raised by :meth:`~niwaki.query.Query.one` /
    :meth:`~niwaki.query.AsyncQuery.one` when the result set is empty.  Use
    :meth:`~niwaki.query.Query.first` when *no match* is an acceptable outcome.
    """


class MultipleResultsError(NiwakiError):
    """A query that required exactly one object matched more than one.

    Raised by :meth:`~niwaki.query.Query.one` /
    :meth:`~niwaki.query.AsyncQuery.one` when the result set holds two or more
    objects.  Narrow the query, or use :meth:`~niwaki.query.Query.first` /
    :meth:`~niwaki.query.Query.fetch` when several matches are expected.
    """


class UnknownClassError(NiwakiError, KeyError):
    """A class name the read catalogue does not know.

    Raised by :func:`niwaki.catalog.describe`,
    :func:`niwaki.catalog.class_meta` and :func:`niwaki.catalog.prop_meta`
    when the wire class name (or property) does not exist in the shipped
    catalogue — usually a typo, or a class minted by a newer APIC firmware
    than the one this build tracks.

    Also a :class:`KeyError`: callers that guarded these lookups with
    ``except KeyError`` before this class existed keep working unchanged —
    the same dual-inheritance precedent as
    :class:`~niwaki.exceptions.UnknownMakerError`.
    """
