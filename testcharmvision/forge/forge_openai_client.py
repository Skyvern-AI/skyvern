import asyncio

from openai import DefaultAsyncHttpxClient


class ForgeAsyncHttpxClientWrapper(DefaultAsyncHttpxClient):
    """
    Wrapper around OpenAI's AsyncHttpxClientWrapper to mask teardown races.

    The upstream `__del__` checks `self.is_closed`, but during interpreter
    shutdown httpx internals may already be None, which raises:

    AttributeError: 'NoneType' object has no attribute 'CLOSED'

    We defensively swallow that destructor error so shutdown logs stay clean.
    """

    def __del__(self) -> None:
        try:
            if self.is_closed:
                return
            asyncio.get_running_loop().create_task(self.aclose())
        except Exception:
            pass
