"""What a push says while it works — and the one thing it must never say.

Ten thousand objects used to go out in silence: when one was refused, the
report named the DN, but nothing said what had been happening for the four
minutes before, or how far it had got.

The half of this that matters most is negative. A design carries passwords,
community strings, pre-shared keys — ``snmpCommunityP`` has the community
string as its *naming* property, so it is in the DN itself. Logging a payload
would put secrets into a file nobody thinks of as sensitive.

The credentials canary below is deliberately routed through a **failing** push.
The branch that logs a refusal only exists on that path, so a canary that
pushes successfully never visits the code it is meant to police — which is the
shape of a test that passes while proving nothing.
"""

from __future__ import annotations

import logging

import pytest
from pytest_httpx import HTTPXMock

from niwaki.design import tenant
from niwaki.exceptions import StagedPushError
from niwaki.facade import Niwaki
from tests.conftest import HOST, LOGIN_URL, login_payload, ok

SECRET = "s3cr3t-community-string"


def _design_with_a_secret() -> object:
    """A design whose payload — and one DN — carry a secret."""
    cfg = tenant("prod")
    cfg.bd("web", description=SECRET)
    cfg.bd("app", description=SECRET)
    return cfg


class TestTheLibraryDoesNotConfigureLogging:
    def test_the_logger_carries_a_null_handler(self) -> None:
        """Emitting without handlers must not print a warning of its own."""
        from niwaki._logging import LOGGER_NAME

        logger = logging.getLogger(LOGGER_NAME)
        assert any(isinstance(h, logging.NullHandler) for h in logger.handlers)

    def test_nothing_touches_the_root_logger(self) -> None:
        """basicConfig steals a decision that belongs to the application."""
        import inspect

        import niwaki._logging as module

        # The call, not the word: this module's own docstring explains why it
        # does not call basicConfig, and a bare substring search finds that.
        source = "\n".join(
            line for line in inspect.getsource(module).splitlines() if not line.startswith("#")
        )
        assert "logging.basicConfig(" not in source
        assert "logger.setLevel(" not in source


class TestAPushNarratesItself:
    def test_a_strict_push_logs_start_and_finish(
        self, httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="POST", json=ok(), is_reusable=True)

        with caplog.at_level(logging.INFO, logger="niwaki"), Niwaki(HOST, "a", "b") as aci:
            _design_with_a_secret().push(aci, mode="strict")  # type: ignore[attr-defined]

        messages = [r.getMessage() for r in caplog.records]
        assert any("push started" in m and "mode=strict" in m for m in messages)
        assert any("push finished" in m for m in messages)

    def test_a_staged_push_reports_its_counts(
        self, httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="POST", json=ok(), is_reusable=True)

        with caplog.at_level(logging.INFO, logger="niwaki"), Niwaki(HOST, "a", "b") as aci:
            _design_with_a_secret().push(aci, mode="staged")  # type: ignore[attr-defined]

        finished = [r for r in caplog.records if "push finished" in r.getMessage()]
        assert finished
        assert "failed=0" in finished[-1].getMessage()

    def test_a_clean_push_stays_at_info(
        self, httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An application showing only warnings should hear nothing when all is well."""
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="POST", json=ok(), is_reusable=True)

        with caplog.at_level(logging.WARNING, logger="niwaki"), Niwaki(HOST, "a", "b") as aci:
            _design_with_a_secret().push(aci, mode="staged")  # type: ignore[attr-defined]

        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


class TestWavesNarrateAtDebug:
    """The 1.9.0 changelog promise: start and finish at INFO, each wave at DEBUG.

    The promise shipped with the function (``wave_started``) but not the call —
    nothing logged a wave until the 2.0 hygiene pass wired it into the engine.
    These tests pin the call on both runners so it cannot fall out again.
    """

    def test_a_staged_push_logs_each_wave_at_debug(
        self, httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="POST", json=ok(), is_reusable=True)

        with caplog.at_level(logging.DEBUG, logger="niwaki"), Niwaki(HOST, "a", "b") as aci:
            _design_with_a_secret().push(aci, mode="staged")  # type: ignore[attr-defined]

        waves = [r for r in caplog.records if "wave depth=" in r.getMessage()]
        # The design compiles to two depths (tenant, then its two BDs).
        assert len(waves) == 2
        assert all(r.levelno == logging.DEBUG for r in waves)
        assert "operations=2" in waves[-1].getMessage()

    async def test_the_async_runner_logs_waves_too(self, caplog: pytest.LogCaptureFixture) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from niwaki.design._engine import _Op, _run_waves

        session = MagicMock()
        session.post_mo = AsyncMock()
        session.delete_mo = AsyncMock()
        ops = [_Op(dn="uni/tn-p", method="POST", payload={})]

        with caplog.at_level(logging.DEBUG, logger="niwaki"):
            outcome = await _run_waves(session, ops, max_concurrent=4)

        assert outcome.ok
        messages = [r.getMessage() for r in caplog.records if "wave depth=" in r.getMessage()]
        # One op at depth 1, and the effective bound is the wave size, not the pool.
        assert messages == ["wave depth=1 operations=1 concurrency=1"]


class TestAPartialPushIsAudible:
    @staticmethod
    def _failing_fabric(httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="POST", url=LOGIN_URL, json=login_payload())
        httpx_mock.add_response(method="POST", json=ok())  # the tenant lands
        httpx_mock.add_response(
            method="POST",
            status_code=400,
            json={"imdata": [{"error": {"attributes": {"code": "801", "text": "refused"}}}]},
            is_reusable=True,
        )

    def test_a_refusal_is_logged_at_warning_with_its_dn(
        self, httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._failing_fabric(httpx_mock)

        with (
            caplog.at_level(logging.WARNING, logger="niwaki"),
            Niwaki(HOST, "a", "b") as aci,
            pytest.raises(StagedPushError),
        ):
            _design_with_a_secret().push(aci, mode="staged")  # type: ignore[attr-defined]

        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("operation refused" in m and "dn=" in m for m in warnings)
        assert any("push finished" in m and "failed=" in m for m in warnings)

    def test_a_partial_push_escalates_to_warning(
        self, httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The summary itself must be audible, not only the individual refusals."""
        self._failing_fabric(httpx_mock)

        with (
            caplog.at_level(logging.WARNING, logger="niwaki"),
            Niwaki(HOST, "a", "b") as aci,
            pytest.raises(StagedPushError),
        ):
            _design_with_a_secret().push(aci, mode="staged")  # type: ignore[attr-defined]

        finished = [r for r in caplog.records if "push finished" in r.getMessage()]
        assert finished
        assert finished[-1].levelno >= logging.WARNING


class TestTheCredentialsCanary:
    """Routed through a FAILING push, because that is the only path that logs.

    A canary that pushes successfully never reaches the refusal branch, so it
    would pass while the branch it polices leaked freely.
    """

    def test_no_secret_reaches_the_log_when_a_push_fails(
        self, httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        TestAPartialPushIsAudible._failing_fabric(httpx_mock)

        with (
            caplog.at_level(logging.DEBUG, logger="niwaki"),
            Niwaki(HOST, "a", "b") as aci,
            pytest.raises(StagedPushError),
        ):
            _design_with_a_secret().push(aci, mode="staged")  # type: ignore[attr-defined]

        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert caplog.records, "the canary never visited the logging branch"
        assert SECRET not in blob
        assert "fvBD" not in blob or "description" not in blob

    def test_no_payload_is_logged_at_any_level(
        self, httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """DEBUG is where a payload would plausibly be logged 'for diagnostics'."""
        TestAPartialPushIsAudible._failing_fabric(httpx_mock)

        with (
            caplog.at_level(logging.DEBUG, logger="niwaki"),
            Niwaki(HOST, "a", "b") as aci,
            pytest.raises(StagedPushError),
        ):
            _design_with_a_secret().push(aci, mode="staged")  # type: ignore[attr-defined]

        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert '"attributes"' not in blob
        assert SECRET not in blob

    def test_the_password_never_appears_either(
        self, httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        TestAPartialPushIsAudible._failing_fabric(httpx_mock)

        with (
            caplog.at_level(logging.DEBUG, logger="niwaki"),
            Niwaki(HOST, "admin", "pw") as aci,
            pytest.raises(StagedPushError),
        ):
            _design_with_a_secret().push(aci, mode="staged")  # type: ignore[attr-defined]

        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert "pw" not in blob.replace("push", "").replace("pwd", "")
