"""Tests for niwaki.transport._errors — shared HTTP error helpers.

Covers: extract_apic_error (standard payload, malformed payload, raw text fallback)
and raise_for_apic_status (2xx no-op, 401, 403, 404, 5xx, other 4xx).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from niwaki import exceptions
from niwaki.transport._errors import extract_apic_error, raise_for_apic_status

# ── Helpers ───────────────────────────────────────────────────────────────────


def _resp(status: int, *, json: dict[str, Any] | None = None, text: str = "") -> MagicMock:
    """Build a minimal httpx.Response-like mock."""
    resp = MagicMock()
    resp.status_code = status
    resp.is_success = 200 <= status < 300
    resp.text = text
    if json is not None:
        resp.json.return_value = json
    else:
        resp.json.side_effect = ValueError("no json")
    return resp


def _apic_error(code: str = "401", text: str = "Unauthorized") -> dict[str, Any]:
    return {"imdata": [{"error": {"attributes": {"code": code, "text": text}}}]}


# ── extract_apic_error ────────────────────────────────────────────────────────


class TestExtractApicError:
    def test_standard_payload_returns_text(self) -> None:
        resp = _resp(401, json=_apic_error(text="Username is wrong"))
        assert extract_apic_error(resp) == "Username is wrong"

    def test_missing_error_key_falls_back_to_text(self) -> None:
        resp = _resp(500, json={"totalCount": "0", "imdata": []}, text="Internal Server Error")
        result = extract_apic_error(resp)
        assert result == "Internal Server Error"

    def test_malformed_json_falls_back_to_raw_text(self) -> None:
        resp = _resp(400, text="bad request body here that is long" * 10)
        result = extract_apic_error(resp)
        assert len(result) <= 200

    def test_json_parse_error_falls_back_to_text(self) -> None:
        resp = _resp(503, text="Service unavailable")
        # json() raises — falls back to .text
        result = extract_apic_error(resp)
        assert result == "Service unavailable"

    def test_empty_imdata_falls_back_to_text(self) -> None:
        resp = _resp(400, json={"imdata": []}, text="empty imdata")
        assert extract_apic_error(resp) == "empty imdata"

    def test_text_truncated_to_200_chars(self) -> None:
        resp = _resp(500, text="x" * 500)
        assert len(extract_apic_error(resp)) == 200


# ── raise_for_apic_status ─────────────────────────────────────────────────────


class TestRaiseForApicStatus:
    def test_success_200_does_not_raise(self) -> None:
        resp = _resp(200, json={"totalCount": "0", "imdata": []})
        raise_for_apic_status(resp)  # must not raise

    def test_success_201_does_not_raise(self) -> None:
        resp = _resp(201, json={})
        raise_for_apic_status(resp)

    def test_401_raises_unauthorized(self) -> None:
        resp = _resp(401, json=_apic_error("401", "Unauthorized"))
        with pytest.raises(exceptions.UnauthorizedError) as exc_info:
            raise_for_apic_status(resp)
        assert exc_info.value.status_code == 401

    def test_403_raises_forbidden(self) -> None:
        resp = _resp(403, json=_apic_error("403", "Forbidden"))
        with pytest.raises(exceptions.ForbiddenError) as exc_info:
            raise_for_apic_status(resp)
        assert exc_info.value.status_code == 403

    def test_404_raises_not_found(self) -> None:
        resp = _resp(404, json=_apic_error("404", "Not found"))
        with pytest.raises(exceptions.NotFoundError) as exc_info:
            raise_for_apic_status(resp)
        assert exc_info.value.status_code == 404

    def test_500_raises_server_error(self) -> None:
        resp = _resp(500, json=_apic_error("500", "Server error"))
        with pytest.raises(exceptions.ServerError) as exc_info:
            raise_for_apic_status(resp)
        assert exc_info.value.status_code == 500

    def test_503_raises_server_error(self) -> None:
        resp = _resp(503, text="Service unavailable")
        with pytest.raises(exceptions.ServerError):
            raise_for_apic_status(resp)

    def test_other_4xx_raises_api_error(self) -> None:
        resp = _resp(422, json=_apic_error("422", "Unprocessable"))
        with pytest.raises(exceptions.APIError) as exc_info:
            raise_for_apic_status(resp)
        assert exc_info.value.status_code == 422

    def test_error_message_propagated(self) -> None:
        resp = _resp(403, json=_apic_error("403", "Insufficient privilege level"))
        with pytest.raises(exceptions.ForbiddenError, match="Insufficient privilege level"):
            raise_for_apic_status(resp)

    def test_both_sessions_use_same_mapping(self) -> None:
        """Both sync and async sessions should produce identical error types."""
        from niwaki.transport.session import ApicSession
        from niwaki.transport.session_async import AsyncApicSession

        # Neither session defines _raise_for_status any more — they call the module fn.
        assert not hasattr(ApicSession, "_raise_for_status")
        assert not hasattr(AsyncApicSession, "_raise_for_status")
        assert not hasattr(ApicSession, "_extract_apic_error")
        assert not hasattr(AsyncApicSession, "_extract_apic_error")
