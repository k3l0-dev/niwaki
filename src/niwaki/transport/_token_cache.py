"""Reusing a token across processes, so a CLI does not log in per command.

A long-lived program logs in once and keeps its session. A command-line tool
cannot: every invocation is a fresh process, so every ``niwaki show ...`` would
mint a new APIC session. On a fabric with session limits and audit logging,
that is both wasteful and noisy — an operator running twenty commands appears
in the audit trail twenty times.

The cache is deliberately small and deliberately paranoid:

**It stores a bearer token.** Anyone who reads the file can act as that user
until it expires. So the file is written with owner-only permissions, created
with them rather than fixed afterwards, and its directory too. A cache that is
world-readable is worse than no cache, because it is invisible.

**It is keyed by host and user**, so two fabrics or two accounts never collide,
and the key is hashed rather than spelled out — a directory listing should not
enumerate which fabrics an operator touches.

**It refuses what it cannot trust.** A file whose permissions are wrong, whose
contents do not parse, or whose token has expired is discarded rather than
repaired: the cost of a fresh login is one request, and guessing is how a
cache becomes a security story.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

_OWNER_ONLY_FILE = 0o600
_OWNER_ONLY_DIR = 0o700
# Refuse a token this close to expiry: a cached token that dies mid-command is
# worse than one fresh login, and the caller cannot retry what it never saw.
_MIN_REMAINING_SECONDS = 30.0


def default_cache_dir() -> Path:
    """The directory a token cache lives in, honouring XDG.

    Returns:
        ``$XDG_CACHE_HOME/niwaki`` when that variable is set, else
        ``~/.cache/niwaki``.  The directory is not created here — writing does
        that, with the right permissions from the start.
    """
    root = os.environ.get("XDG_CACHE_HOME")
    base = Path(root) if root else Path.home() / ".cache"
    return base / "niwaki"


def cache_key(host: str, username: str) -> str:
    """A filename-safe key identifying one fabric-and-user pair.

    Hashed rather than spelled out: a listing of the cache directory should not
    reveal which fabrics an operator has been talking to, nor under which
    account.

    Args:
        host: The APIC base URL.
        username: The login name.

    Returns:
        A hexadecimal digest, stable across processes and platforms.

    Example::

        cache_key("https://apic.example.com", "admin")[:8]
    """
    return sha256(f"{host.rstrip('/')}\x00{username}".encode()).hexdigest()


class TokenCache:
    """An on-disk store for APIC tokens, one entry per fabric-and-user.

    Every method fails soft: a cache that cannot be read, written or trusted
    behaves as an empty one, because the fallback — logging in — always works.
    The only hard failure is a caller asking to write somewhere impossible,
    which is a configuration error worth surfacing.

    Attributes:
        directory: Where entries are stored.

    Example::

        cache = TokenCache()
        if (entry := cache.read(host, user)) is not None:
            token, expires_at = entry
    """

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory if directory is not None else default_cache_dir()

    def _path(self, host: str, username: str) -> Path:
        return self.directory / f"{cache_key(host, username)}.json"

    def read(self, host: str, username: str) -> tuple[str, datetime] | None:
        """Return a usable cached token, or ``None``.

        Discards, rather than repairs, an entry that is unreadable, malformed,
        group- or world-accessible, or too near expiry to be worth using.

        Args:
            host: The APIC base URL.
            username: The login name.

        Returns:
            ``(token, expires_at)`` when the entry is trustworthy and has more
            than thirty seconds left, otherwise ``None``.
        """
        path = self._path(host, username)
        try:
            info = path.stat()
        except OSError:
            return None
        if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            # Someone widened it, or it was written by a careless tool. A token
            # others can read is not one to keep using.
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            token = str(payload["token"])
            expires_at = datetime.fromisoformat(str(payload["expires_at"]))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if expires_at.tzinfo is None:
            return None
        remaining = (expires_at - datetime.now(UTC)).total_seconds()
        if remaining <= _MIN_REMAINING_SECONDS:
            return None
        return token, expires_at

    def write(self, host: str, username: str, token: str, expires_at: datetime) -> None:
        """Store a token for later processes, readable only by its owner.

        The file is created with owner-only permissions rather than chmod'ed
        afterwards — between creation and a later chmod there is a window in
        which the token is world-readable, and a window is all it takes.  The
        write is atomic: a replace, so a reader never sees a half-written file.

        Args:
            host: The APIC base URL.
            username: The login name.
            token: The APIC token.
            expires_at: When it stops being valid.  Must be timezone-aware, so
                a cache written in one timezone is read correctly in another.

        Raises:
            ValueError: *expires_at* is naive.
            OSError: The cache directory cannot be created or written.
        """
        if expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        self.directory.mkdir(parents=True, exist_ok=True, mode=_OWNER_ONLY_DIR)
        payload = json.dumps({"token": token, "expires_at": expires_at.isoformat()})

        # mkstemp creates with 0600 and no race; the replace is atomic.
        handle, temporary = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload)
            os.chmod(temporary, _OWNER_ONLY_FILE)
            os.replace(temporary, self._path(host, username))
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def clear(self, host: str, username: str) -> None:
        """Forget one entry.  A missing entry is not an error.

        Args:
            host: The APIC base URL.
            username: The login name.
        """
        self._path(host, username).unlink(missing_ok=True)
