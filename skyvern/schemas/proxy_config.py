from pydantic import BaseModel, Field, SecretStr


class BrowserSessionProxyConfig(BaseModel):
    server: str = Field(
        description="Proxy server URL, for example http://proxy.example.com:8080 or socks5://proxy.example.com:1080.",
    )
    username: str | None = Field(default=None, description="Optional proxy username.")
    password: SecretStr | None = Field(default=None, description="Optional proxy password.", repr=False)
    bypass: str | None = Field(
        default=None,
        description="Optional comma-separated hosts that should bypass the proxy.",
    )

    def to_playwright_proxy(self) -> dict[str, str]:
        proxy: dict[str, str] = {"server": self.server}
        if self.username:
            proxy["username"] = self.username
        if self.password is not None:
            proxy["password"] = self.password.get_secret_value()
        if self.bypass:
            proxy["bypass"] = self.bypass
        return proxy
