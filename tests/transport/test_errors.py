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


# ── apic_code: the APIC's own error code, preserved ───────────────────────────


class TestApicCode:
    """The controller's machine-readable cause, kept alongside the message.

    The APIC answers every failure with ``error.attributes.code``.  It used to
    be dropped on the floor, so a caller had to regex the English text to learn
    *why* a push was refused.  Measured on 6.0(9c): many distinct codes share
    HTTP 400 — a malformed DN, a missing naming property and an out-of-range
    integer are all 400 — so the status alone cannot tell them apart.

    Note the trap these tests are written around: every pre-existing fixture in
    this repo carries ``code == str(status)``, so an assertion made against one
    of them cannot tell ``apic_code`` from ``str(status_code)``.  Every test
    here uses a code that does **not** match its status.
    """

    def test_the_code_survives_when_it_differs_from_the_status(self) -> None:
        resp = _resp(400, json=_apic_error("103", "Object already exists."))
        with pytest.raises(exceptions.APIError) as excinfo:
            raise_for_apic_status(resp)
        assert excinfo.value.apic_code == "103"
        assert excinfo.value.status_code == 400  # the two are independent

    def test_a_body_with_no_error_envelope_gives_none_not_empty_string(self) -> None:
        """``None`` means "the APIC said nothing", ``""`` would mean "it said nothing useful"."""
        resp = _resp(400, json={"imdata": []}, text="upstream said no")
        with pytest.raises(exceptions.APIError) as excinfo:
            raise_for_apic_status(resp)
        assert excinfo.value.apic_code is None

    def test_an_html_body_does_not_raise_out_of_the_error_handler(self) -> None:
        """A simulator under load answers nginx HTML, not JSON — measured live."""
        page = (
            "<html>\n<head><title>404 Not Found</title></head>\n<body>\n"
            "<center><h1>404 Not Found</h1></center>\n<hr><center>Cisco APIC</center>\n"
            "</body>\n</html>"
        )
        resp = _resp(404, text=page)
        with pytest.raises(exceptions.NotFoundError) as excinfo:
            raise_for_apic_status(resp)
        assert excinfo.value.apic_code is None
        assert excinfo.value.apic_message == page[:200]

    @pytest.mark.parametrize(
        "body",
        [
            {"imdata": [{"error": {"attributes": None}}]},
            {"imdata": {"error": {"attributes": {"code": "1"}}}},
            {"imdata": ["not a mapping"]},
        ],
        ids=["attributes-null", "imdata-not-a-list", "first-item-not-a-mapping"],
    )
    def test_degenerate_shapes_fall_back_without_raising(self, body: Any) -> None:
        resp = _resp(400, json=body, text="raw body")
        with pytest.raises(exceptions.APIError) as excinfo:
            raise_for_apic_status(resp)
        assert excinfo.value.apic_code is None
        assert excinfo.value.apic_message == "raw body"

    def test_a_code_without_text_keeps_the_code(self) -> None:
        """The two fields fall back independently — one missing does not sink the other."""
        resp = _resp(400, json={"imdata": [{"error": {"attributes": {"code": "801"}}}]}, text="raw")
        with pytest.raises(exceptions.APIError) as excinfo:
            raise_for_apic_status(resp)
        assert excinfo.value.apic_code == "801"
        assert excinfo.value.apic_message == "raw"

    def test_the_code_is_a_string_never_an_int(self) -> None:
        """Parsing to ``int`` would add a crash path inside the error handler itself."""
        resp = _resp(400, json=_apic_error("120", "Invalid value"))
        with pytest.raises(exceptions.APIError) as excinfo:
            raise_for_apic_status(resp)
        assert excinfo.value.apic_code == "120"
        assert isinstance(excinfo.value.apic_code, str)

    def test_every_status_branch_carries_the_code(self) -> None:
        """Not just the fallback branch — 401/403/404/5xx each pass it through."""
        for status, expected in (
            (401, exceptions.UnauthorizedError),
            (403, exceptions.ForbiddenError),
            (404, exceptions.NotFoundError),
            (503, exceptions.ServerError),
            (422, exceptions.APIError),
        ):
            resp = _resp(status, json=_apic_error("777", "boom"))
            with pytest.raises(expected) as excinfo:
                raise_for_apic_status(resp)
            assert excinfo.value.apic_code == "777", f"status {status} dropped the code"

    def test_the_message_names_the_code_only_when_there_is_one(self) -> None:
        """A synthesised error must keep rendering exactly as it always did."""
        assert str(exceptions.NotFoundError(404, "MO not found")) == "HTTP 404: MO not found"
        assert str(exceptions.APIError(400, "boom", apic_code="103")) == (
            "HTTP 400 (APIC code 103): boom"
        )


class TestJsonData:
    """The data-path twin of the auth paths' body hardening.

    A 2xx whose body is not JSON (the nginx HTML page a simulator under load
    serves) must surface as a typed APIError carrying the real status — never
    a bare ``json.JSONDecodeError``.
    """

    def test_a_json_object_passes_through(self) -> None:
        import httpx

        from niwaki.transport._errors import json_data

        resp = httpx.Response(200, json={"totalCount": "1", "imdata": []})
        assert json_data(resp) == {"totalCount": "1", "imdata": []}

    def test_a_non_json_body_raises_a_typed_error_with_the_real_status(self) -> None:
        import httpx

        from niwaki.transport._errors import json_data

        resp = httpx.Response(200, content=b"<html>bad gateway page</html>")
        with pytest.raises(exceptions.APIError) as excinfo:
            json_data(resp)
        assert excinfo.value.status_code == 200
        assert "non-JSON" in excinfo.value.apic_message

    def test_a_non_object_json_body_is_refused_too(self) -> None:
        import httpx

        from niwaki.transport._errors import json_data

        resp = httpx.Response(200, json=["not", "an", "envelope"])
        with pytest.raises(exceptions.APIError) as excinfo:
            json_data(resp)
        assert "non-object" in excinfo.value.apic_message
