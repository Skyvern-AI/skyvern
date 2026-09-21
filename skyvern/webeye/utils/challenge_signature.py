"""Vendor signature shared by the Task V3 observe scan and the copilot scout click hooks.

Spliced verbatim into a JavaScript regex literal as well as compiled by ``re``, so it must stay
inside the syntax both engines read the same way: no named groups, no lookbehind, no ``/``.
"""

from __future__ import annotations

CHALLENGE_VENDOR_SIGNATURE = (
    r"captcha|turnstile|challenges\.cloudflare|arkoselabs|funcaptcha|datadome|perimeterx"
    r"|verify you are human|security challenge"
)
