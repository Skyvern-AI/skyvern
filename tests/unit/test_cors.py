import httpx
import pytest
from fastapi import FastAPI

from skyvern.cors import cors_allows_any_origin, credentialed_cors_allow_origin_regex, credentialed_cors_allow_origins
from skyvern.forge import api_app as forge_api_app


@pytest.mark.asyncio
async def test_wildcard_allowed_origins_allow_any_origin_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(forge_api_app.settings, "ALLOWED_ORIGINS", ["*"])
    monkeypatch.setattr(forge_api_app.settings, "ALLOWED_ORIGIN_REGEX", None)
    app = FastAPI()

    @app.get("/probe")
    async def probe() -> dict[str, bool]:
        return {"ok": True}

    forge_api_app.add_credentialed_cors_middleware(app)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.options(
            "/probe",
            headers={"Origin": "https://ui.selfhosted.test", "Access-Control-Request-Method": "GET"},
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in response.headers


def test_cors_allows_any_origin_requires_exact_wildcard() -> None:
    assert cors_allows_any_origin(["*"]) is True
    assert cors_allows_any_origin([" * "]) is True
    assert cors_allows_any_origin(["*.example.test"]) is False
    assert cors_allows_any_origin(["https://app.example.test", ""]) is False


def test_credentialed_cors_allow_origins_drops_wildcards() -> None:
    assert credentialed_cors_allow_origins(
        [
            " https://app.example.test ",
            "*",
            "https://*.example.test",
            "",
        ]
    ) == ["https://app.example.test"]


def test_credentialed_cors_allow_origin_regex_normalizes_blank_values() -> None:
    assert credentialed_cors_allow_origin_regex(None) is None
    assert credentialed_cors_allow_origin_regex("   ") is None
    assert credentialed_cors_allow_origin_regex(r" \Ahttps://app\.example\.test\Z ") == (
        r"\Ahttps://app\.example\.test\Z"
    )
