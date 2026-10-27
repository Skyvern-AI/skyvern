import pytest

from skyvern.webeye.utils.recaptcha_variant import classify_recaptcha_widget, recaptcha_anchor_target

_API2_ANCHOR = "https://www.google.com/recaptcha/api2/anchor?ar=1&k=key-a&size="
_ENTERPRISE_ANCHOR = "https://www.recaptcha.net/recaptcha/enterprise/anchor?ar=1&k=key-a&size="


@pytest.mark.parametrize(
    ("anchors", "loaders", "expected"),
    (
        ([("api2", "normal")], (), "recaptcha_v2_checkbox"),
        ([("api2", "compact"), ("api2", "normal")], (), "recaptcha_v2_checkbox"),
        ([("api2", "invisible")], ("api2",), "recaptcha_v3"),
        ([], ("api2",), "recaptcha_v3"),
        ([("enterprise", "invisible")], ("enterprise",), "recaptcha_enterprise"),
        ([("enterprise", "normal")], (), "recaptcha_enterprise"),
        # Google documents v3 keys rendered without a score loader (button binding, or the Invisible API with
        # size invisible), so an invisible widget without its own loader may be either version.
        ([("api2", "invisible")], (), "unknown"),
        ([("api2", "normal")], ("api2",), "unknown"),
        ([("api2", "invisible")], ("enterprise",), "unknown"),
        ([("enterprise", "normal"), ("api2", "normal")], (), "unknown"),
        ([("api2", None)], (), "unknown"),
        ([], (), "unknown"),
    ),
)
def test_a_recaptcha_widget_is_classified_only_from_evidence_bound_to_it(
    anchors: list[tuple[str, str | None]], loaders: tuple[str, ...], expected: str
) -> None:
    assert classify_recaptcha_widget(anchors, loaders) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    (
        (_API2_ANCHOR + "normal", "recaptcha_v2_checkbox"),
        (_API2_ANCHOR + "compact", "recaptcha_v2_checkbox"),
        (_ENTERPRISE_ANCHOR + "normal", "recaptcha_enterprise"),
        (_ENTERPRISE_ANCHOR + "invisible", "recaptcha_enterprise"),
        (_API2_ANCHOR + "invisible", "unknown"),
        (_API2_ANCHOR + "normal&size=invisible", "unknown"),
        ("http://www.google.com/recaptcha/api2/anchor?k=key-a&size=normal", "unknown"),
        ("https://evil.example/recaptcha/api2/anchor?k=key-a&size=normal", "unknown"),
        ("https://www.google.com/recaptcha/api2/bframe?k=key-a", "unknown"),
        ("https://[::1/recaptcha/api2/anchor", "unknown"),
        (None, "unknown"),
    ),
)
def test_an_anchor_url_alone_proves_only_a_checkbox_or_enterprise(url: str | None, expected: str) -> None:
    assert recaptcha_anchor_target(url) == expected
