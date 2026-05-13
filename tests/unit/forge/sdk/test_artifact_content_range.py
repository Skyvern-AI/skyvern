from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skyvern.forge import app as forge_app
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.routes import routers as routers_module
from skyvern.forge.sdk.routes.agent_protocol import _parse_range_header

CONTENT = b"0123456789" * 100  # 1000 bytes for predictable slicing


def _build_client(monkeypatch: pytest.MonkeyPatch, *, content: bytes = CONTENT) -> TestClient:
    async def _fake_org(**_: object) -> SimpleNamespace:
        return SimpleNamespace(organization_id="org_oss")

    artifact = SimpleNamespace(
        artifact_id="a_42",
        artifact_type=ArtifactType.RECORDING,
        uri="s3://bucket/path/recording.webm",
        organization_id="org_oss",
    )

    artifacts_db = MagicMock()
    artifacts_db.get_artifact_by_id = AsyncMock(return_value=artifact)
    artifacts_db.get_artifact_by_id_no_org = AsyncMock(return_value=artifact)
    monkeypatch.setattr(forge_app.DATABASE, "artifacts", artifacts_db)
    monkeypatch.setattr(
        forge_app.ARTIFACT_MANAGER,
        "retrieve_artifact",
        AsyncMock(return_value=content),
    )
    # The endpoint calls get_current_org directly (not via Depends), so patch
    # the symbol on the route module rather than the service module.
    from skyvern.forge.sdk.routes import agent_protocol

    monkeypatch.setattr(agent_protocol.org_auth_service, "get_current_org", _fake_org)

    fastapi_app = FastAPI()
    fastapi_app.include_router(routers_module.base_router, prefix="/v1")
    return TestClient(fastapi_app)


class TestParseRangeHeader:
    def test_none_header_returns_none(self) -> None:
        assert _parse_range_header(None, 1000) is None

    def test_empty_header_returns_none(self) -> None:
        assert _parse_range_header("", 1000) is None

    def test_non_bytes_unit_returns_none(self) -> None:
        assert _parse_range_header("items=0-99", 1000) is None

    def test_full_range(self) -> None:
        assert _parse_range_header("bytes=0-99", 1000) == (0, 99)

    def test_open_ended_range(self) -> None:
        assert _parse_range_header("bytes=500-", 1000) == (500, 999)

    def test_open_ended_range_from_zero(self) -> None:
        assert _parse_range_header("bytes=0-", 1000) == (0, 999)

    def test_suffix_range_last_n_bytes(self) -> None:
        assert _parse_range_header("bytes=-100", 1000) == (900, 999)

    def test_suffix_range_larger_than_content_clamps_to_zero(self) -> None:
        assert _parse_range_header("bytes=-5000", 1000) == (0, 999)

    def test_end_beyond_content_is_clamped(self) -> None:
        assert _parse_range_header("bytes=900-9999", 1000) == (900, 999)

    def test_unsatisfiable_start_returns_sentinel(self) -> None:
        assert _parse_range_header("bytes=2000-3000", 1000) == (-1, -1)

    def test_inverted_range_returns_sentinel(self) -> None:
        assert _parse_range_header("bytes=500-100", 1000) == (-1, -1)

    def test_multipart_range_returns_none(self) -> None:
        # Multipart ranges (RFC 7233 §4.1) are valid but we don't implement them.
        assert _parse_range_header("bytes=0-100,200-300", 1000) is None

    def test_malformed_returns_none(self) -> None:
        assert _parse_range_header("bytes=abc", 1000) is None
        assert _parse_range_header("bytes=", 1000) is None
        assert _parse_range_header("bytes=10-abc", 1000) is None

    def test_zero_length_content(self) -> None:
        assert _parse_range_header("bytes=0-0", 0) == (-1, -1)

    def test_suffix_zero_returns_none(self) -> None:
        assert _parse_range_header("bytes=-0", 1000) is None

    def test_negative_position_treated_as_malformed(self) -> None:
        # "0--1" parses to start=0, end=-1; per RFC 7233 §3.1 malformed Range
        # headers must be ignored, not treated as unsatisfiable.
        assert _parse_range_header("bytes=0--1", 1000) is None
        assert _parse_range_header("bytes=-1-100", 1000) is None
        assert _parse_range_header("bytes=+5-100", 1000) is None

    def test_non_ascii_digits_rejected(self) -> None:
        # RFC 5234 byte-pos is ASCII DIGIT only; Unicode decimal digits like
        # Devanagari (० १) pass str.isdigit() but must not be accepted.
        assert _parse_range_header("bytes=०-१", 1000) is None
        assert _parse_range_header("bytes=0-१", 1000) is None
        assert _parse_range_header("bytes=-१", 1000) is None

    def test_case_insensitive_unit(self) -> None:
        # HTTP field tokens are case-insensitive (RFC 7230 §3.2.6).
        assert _parse_range_header("Bytes=0-99", 1000) == (0, 99)
        assert _parse_range_header("BYTES=500-", 1000) == (500, 999)

    def test_leading_whitespace_tolerated(self) -> None:
        assert _parse_range_header("  bytes=0-99", 1000) == (0, 99)


class TestArtifactContentRangeRequests:
    def test_no_range_returns_full_content_with_accept_ranges(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _build_client(monkeypatch)
        response = client.get("/v1/artifacts/a_42/content")

        assert response.status_code == 200
        assert response.content == CONTENT
        assert response.headers["accept-ranges"] == "bytes"
        assert "content-range" not in {k.lower() for k in response.headers}

    def test_range_request_returns_206_partial_content(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _build_client(monkeypatch)
        response = client.get("/v1/artifacts/a_42/content", headers={"Range": "bytes=10-19"})

        assert response.status_code == 206
        assert response.content == CONTENT[10:20]
        assert response.headers["content-range"] == f"bytes 10-19/{len(CONTENT)}"
        assert response.headers["accept-ranges"] == "bytes"

    def test_open_ended_range(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _build_client(monkeypatch)
        response = client.get("/v1/artifacts/a_42/content", headers={"Range": "bytes=900-"})

        assert response.status_code == 206
        assert response.content == CONTENT[900:]
        assert response.headers["content-range"] == f"bytes 900-999/{len(CONTENT)}"

    def test_suffix_range(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _build_client(monkeypatch)
        response = client.get("/v1/artifacts/a_42/content", headers={"Range": "bytes=-50"})

        assert response.status_code == 206
        assert response.content == CONTENT[-50:]
        assert response.headers["content-range"] == f"bytes 950-999/{len(CONTENT)}"

    def test_unsatisfiable_range_returns_416(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _build_client(monkeypatch)
        response = client.get("/v1/artifacts/a_42/content", headers={"Range": "bytes=5000-6000"})

        assert response.status_code == 416
        assert response.headers["content-range"] == f"bytes */{len(CONTENT)}"

    def test_invalid_range_falls_through_to_200(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Per RFC 7233 §3.1, unparseable Range headers MUST be ignored.
        client = _build_client(monkeypatch)
        response = client.get("/v1/artifacts/a_42/content", headers={"Range": "bytes=garbage"})

        assert response.status_code == 200
        assert response.content == CONTENT
        assert response.headers["accept-ranges"] == "bytes"
