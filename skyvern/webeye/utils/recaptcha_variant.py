"""Which reCAPTCHA variant one widget is, read only from evidence bound to that widget: the URL of its anchor frame
and a loader script whose ``render`` names its key. Pure and non-raising; never returns a URL, key or page text."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Literal
from urllib.parse import parse_qs, urlsplit

RecaptchaTarget = Literal["recaptcha_v2_checkbox", "recaptcha_v3", "recaptcha_enterprise", "unknown"]
RecaptchaFamily = Literal["api2", "enterprise"]

_HOSTS = ("www.google.com", "www.recaptcha.net")
_ANCHOR_PATHS: dict[str, RecaptchaFamily] = {
    "/recaptcha/api2/anchor": "api2",
    "/recaptcha/enterprise/anchor": "enterprise",
}
_LOADER_PATHS: dict[str, RecaptchaFamily] = {"/recaptcha/api.js": "api2", "/recaptcha/enterprise.js": "enterprise"}
_CHECKBOX_SIZES = frozenset({"normal", "compact"})


def _query(url: object, paths: dict[str, RecaptchaFamily]) -> tuple[RecaptchaFamily, dict[str, list[str]]] | None:
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        hostname = (parts.hostname or "").lower()
    except ValueError:
        return None
    family = paths.get(parts.path)
    if parts.scheme != "https" or hostname not in _HOSTS or family is None:
        return None
    return family, parse_qs(parts.query, keep_blank_values=True)


def _single(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name, [])
    return values[0] if len(values) == 1 else None


def recaptcha_anchor(url: object) -> tuple[RecaptchaFamily, str | None, str | None] | None:
    """``(family, size, key)`` of a trusted anchor-frame URL; a parameter not given exactly once is None."""
    parsed = _query(url, _ANCHOR_PATHS)
    if parsed is None:
        return None
    family, query = parsed
    return family, _single(query, "size"), _single(query, "k")


def recaptcha_loader(url: object) -> tuple[RecaptchaFamily, str | None] | None:
    """``(family, render)`` of a trusted loader-script URL."""
    parsed = _query(url, _LOADER_PATHS)
    if parsed is None:
        return None
    family, query = parsed
    return family, _single(query, "render")


def classify_recaptcha_widget(
    anchors: Sequence[tuple[str, str | None]], bound_loaders: Iterable[str] = ()
) -> RecaptchaTarget:
    """The variant of one widget from its own anchor frames, as ``(family, size)``, and the families of the loaders
    whose ``render`` is its key. Google documents v3 keys rendered without such a loader, including through the
    Invisible API, so an invisible widget without one stays unknown."""
    sizes = {size for _, size in anchors}
    loaders = set(bound_loaders)
    families = {family for family, _ in anchors} | loaders
    if families == {"enterprise"}:
        return "recaptcha_enterprise"
    if families != {"api2"}:
        return "unknown"
    if loaders:
        return "recaptcha_v3" if sizes <= {"invisible"} else "unknown"
    return "recaptcha_v2_checkbox" if sizes and sizes <= _CHECKBOX_SIZES else "unknown"


def recaptcha_anchor_target(url: object) -> RecaptchaTarget:
    anchor = recaptcha_anchor(url)
    if anchor is None:
        return "unknown"
    family, size, _ = anchor
    return classify_recaptcha_widget([(family, size)])
