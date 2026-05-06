def configure_litellm_transport() -> None:
    import litellm

    # litellm's aiohttp transport drops per-request timeout; httpx honors it.
    litellm.disable_aiohttp_transport = True
