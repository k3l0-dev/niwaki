"""What the SDK says about itself while it works, and what it refuses to say.

Ten thousand objects used to go out in silence. When one of them was refused
the report named the DN, but nothing said what the SDK had been doing for the
preceding four minutes, or how far it had got.

Two rules shape this module.

**The library never configures logging.** It attaches a ``NullHandler`` and
emits records; whether they are shown, and where, belongs to the application.
A library that calls ``basicConfig`` steals a decision that is not its own.

**A payload is never logged.** A design carries passwords, community strings,
pre-shared keys — ``snmpCommunityP`` even has the community string as its
*naming* property, so it is in the DN. Logging a payload at ``DEBUG`` would put
secrets in a file an operator did not think of as sensitive. So the SDK logs
what it did and where, never what it sent.
"""

from __future__ import annotations

import logging
from typing import Final

#: The one logger the SDK emits under.  Applications configure this name, or
#: any prefix of it, and get everything below.
LOGGER_NAME: Final = "niwaki"

logger = logging.getLogger(LOGGER_NAME)
# A library that does not configure logging still must not warn about missing
# handlers when the application has not configured any.
logger.addHandler(logging.NullHandler())


def push_started(mode: str, count: int) -> None:
    """Record that a push is beginning.

    Args:
        mode: ``strict``, ``staged`` or ``plan``.
        count: How many operations the design compiled to.
    """
    logger.info("push started: mode=%s operations=%d", mode, count)


def wave_started(depth: int, width: int, bound: int) -> None:
    """Record the start of one wave of a staged push.

    Args:
        depth: The DN depth this wave sits at.
        width: How many operations the wave holds.
        bound: How many of them may be in flight at once.
    """
    logger.debug("wave depth=%d operations=%d concurrency=%d", depth, width, bound)


def push_finished(mode: str, succeeded: int, failed: int, not_run: int) -> None:
    """Record how a push ended.

    Logged at ``INFO`` when everything landed and ``WARNING`` otherwise, so an
    application that shows only warnings still hears about a partial push.

    Args:
        mode: The push mode.
        succeeded: Operations the controller accepted.
        failed: Operations it refused.
        not_run: Operations skipped because an ancestor failed.
    """
    level = logging.INFO if not failed and not not_run else logging.WARNING
    logger.log(
        level,
        "push finished: mode=%s succeeded=%d failed=%d skipped=%d",
        mode,
        succeeded,
        failed,
        not_run,
    )


def operation_failed(dn: str, error: BaseException) -> None:
    """Record one refused operation, by DN and by cause.

    The DN and the exception, never the payload: a design's attributes are
    where the secrets are, and this is the branch a failure walks through.

    Args:
        dn: The DN the controller refused.
        error: What it raised.
    """
    logger.warning("operation refused: dn=%s error=%s", dn, error)
