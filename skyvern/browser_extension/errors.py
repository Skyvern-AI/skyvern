class BrowserExtensionError(Exception):
    pass


class BrowserExtensionNotConnectedError(BrowserExtensionError):
    pass


class ExtensionRequestError(BrowserExtensionError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")
