from abc import ABC, abstractmethod
from datetime import timedelta
from typing import Any

CACHE_EXPIRE_TIME = timedelta(weeks=1)
MAX_CACHE_ITEM = 1000


class BaseCache(ABC):
    @abstractmethod
    async def set(self, key: str, value: Any) -> None:
        pass

    @abstractmethod
    async def get(self, key: str) -> Any:
        pass
