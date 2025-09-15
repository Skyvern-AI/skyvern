
from functools import lru_cache

from litellm import model_cost
from pydantic import BaseModel, field_validator


@lru_cache(maxsize=1)
def get_model_names() -> list[str]:
    return list(sorted(model_cost.keys()))


class LLM(BaseModel):
    model_name: str

    @field_validator('model_name')
    @classmethod
    def validate_model_name(cls, v: str) -> str:
        valid_names = set(get_model_names())
        if v not in valid_names:
            raise ValueError(f"Invalid model name '{v}'. Must be a valid litellm model name.")
        return v
