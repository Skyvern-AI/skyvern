from skyvern.cors import credentialed_cors_allow_origin_regex, credentialed_cors_allow_origins


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
