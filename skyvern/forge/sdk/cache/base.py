from abc import ABC, abstractmethod
from datetime import timedelta
from typing import Any, Union

CACHE_EXPIRE_TIME = timedelta(weeks=4)
MAX_CACHE_ITEM = 1000


class BaseCache(ABC):
    @abstractmethod
    async def set(self, key: str, value: Any, ex: Union[int, timedelta, None] = CACHE_EXPIRE_TIME) -> None:
        pass

    @abstractmethod
    async def get(self, key: str) -> Any:
        pass
