from enum import StrEnum


class BotProtection(StrEnum):
    """A service a site uses to tell people from automated browsers, such as a captcha."""

    RECAPTCHA = "recaptcha"
    HCAPTCHA = "hcaptcha"
    CLOUDFLARE = "cloudflare"
    AKAMAI = "akamai"
