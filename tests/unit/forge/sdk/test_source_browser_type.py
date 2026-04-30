from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserType


class TestPersistentBrowserTypeFromSourceBrowserType:
    def test_chrome(self) -> None:
        assert PersistentBrowserType.from_source_browser_type("chrome") == PersistentBrowserType.Chrome

    def test_msedge(self) -> None:
        assert PersistentBrowserType.from_source_browser_type("msedge") == PersistentBrowserType.MSEdge

    def test_stealth_chromium(self) -> None:
        assert (
            PersistentBrowserType.from_source_browser_type("stealth-chromium") == PersistentBrowserType.StealthChromium
        )

    def test_unknown_returns_none(self) -> None:
        assert PersistentBrowserType.from_source_browser_type("unknown-browser") is None

    def test_empty_string_returns_none(self) -> None:
        assert PersistentBrowserType.from_source_browser_type("") is None
