class BrowserExtensionError(Exception):
    pass


class BrowserExtensionBrokerError(BrowserExtensionError):
    """A structured, sanitized broker failure safe to show to a local caller."""

    def __init__(self, code: str, message: str, *, retry_after: float | None = None) -> None:
        self.code = code
        self.message = message
        self.retry_after = retry_after
        super().__init__(f"{code}: {message}")


class BrowserExtensionNotConnectedError(BrowserExtensionError):
    pass


class ExtensionRequestError(BrowserExtensionError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")
