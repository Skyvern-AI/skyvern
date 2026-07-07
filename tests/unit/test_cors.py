from skyvern.cors import credentialed_cors_allow_origins


def test_credentialed_cors_allow_origins_drops_wildcards() -> None:
    assert credentialed_cors_allow_origins(
        [
            " https://app.example.test ",
            "*",
            "https://*.example.test",
            "",
        ]
    ) == ["https://app.example.test"]
