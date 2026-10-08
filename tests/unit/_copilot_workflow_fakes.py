from types import SimpleNamespace


def fake_workflow(**fields: object) -> SimpleNamespace:
    return SimpleNamespace(
        **{
            "extra_http_headers": None,
            "cdp_connect_headers": None,
            "totp_identifier": None,
            "totp_verification_url": None,
            "webhook_callback_url": None,
            "proxy_location": None,
            **fields,
        }
    )
