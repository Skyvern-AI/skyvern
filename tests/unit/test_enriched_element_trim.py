import pytest

from skyvern.forge.sdk.core.skyvern_context import EnrichTreeMode, SkyvernContext
from skyvern.webeye.scraper import scraper


@pytest.fixture
def sample_attributes() -> dict[str, str]:
    return {
        "unique_id": "AAAB",
        "id": "email",
        "name": "email",
        "type": "email",
        "aria-invalid": "true",
        "aria-describedby": "email-error",
        "invalid": "true",
        "validationMessage": "Please enter a valid email",
        "errorText": "Invalid email address",
        "aria-expanded": "false",
        "data-testid": "should-drop",
    }


def test_trimmed_attributes_drops_enriched_fields_in_control(
    monkeypatch: pytest.MonkeyPatch,
    sample_attributes: dict[str, str],
) -> None:
    monkeypatch.setattr(
        scraper.skyvern_context,
        "current",
        lambda: SkyvernContext(enrich_tree_mode=EnrichTreeMode.CONTROL),
    )

    trimmed = scraper._trimmed_attributes(sample_attributes)

    assert trimmed == {
        "name": "email",
        "type": "email",
    }


def test_trimmed_attributes_keeps_enriched_fields_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    sample_attributes: dict[str, str],
) -> None:
    monkeypatch.setattr(
        scraper.skyvern_context,
        "current",
        lambda: SkyvernContext(enrich_tree_mode=EnrichTreeMode.ENRICHED_TREE),
    )

    trimmed = scraper._trimmed_attributes(sample_attributes)

    assert trimmed["validationMessage"] == "Please enter a valid email"
    assert trimmed["errorText"] == "Invalid email address"
    assert trimmed["aria-invalid"] == "true"
    assert trimmed["aria-describedby"] == "email-error"
    assert trimmed["invalid"] == "true"
    assert trimmed["aria-expanded"] == "false"
    assert "data-testid" not in trimmed
