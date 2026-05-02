import litellm

# litellm's aiohttp_transport drops per-request timeout; httpx default honors it.
litellm.disable_aiohttp_transport = True
