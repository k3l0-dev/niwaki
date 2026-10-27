"""Password resolution shared by the sync and async APIC sessions.

The sessions hold the APIC password as a :class:`pydantic.SecretStr` from the
moment it is resolved, and unwrap it only to build the aaaLogin request body.
This module is the one place that turns what the caller handed in (a ``str``,
a ``SecretStr`` or nothing) into that held secret.
"""

from __future__ import annotations

import os

from pydantic import SecretStr


def resolve_password(password: str | SecretStr | None, *, signed: bool) -> SecretStr:
    """Resolve the session password into a ``SecretStr``, without unwrapping it.

    An empty value — ``None``, ``""`` or ``SecretStr("")`` — means "not
    given": a password session then reads ``APIC_PASSWORD``, while a signed
    (certificate) session needs no password at all and keeps an empty one.
    A ``SecretStr`` is kept as is, never wrapped a second time.

    Args:
        password: What the caller passed to the session constructor.
        signed: Whether the session authenticates by certificate signature,
            in which case no password is ever sent and the environment is
            not consulted.

    Returns:
        The secret to hold: the caller's own ``SecretStr`` when one was
        given and non-empty, otherwise a new wrapper around the plain value,
        the environment value, or ``""`` for a signed session.

    Raises:
        KeyError: A password session was given no password and
            ``APIC_PASSWORD`` is not set.

    Example::

        resolve_password("s3cret", signed=False)          # SecretStr('**********')
        resolve_password(SecretStr(""), signed=True)      # SecretStr('')
        resolve_password(None, signed=False)              # reads APIC_PASSWORD
    """
    secret = password if isinstance(password, SecretStr) else SecretStr(password or "")
    if len(secret) or signed:
        return secret
    return SecretStr(os.environ["APIC_PASSWORD"])
