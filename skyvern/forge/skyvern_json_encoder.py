import json
from typing import Any


class SkyvernJSONLogEncoder(json.JSONEncoder):
    """Custom JSON encoder for Skyvern logs that handles non-serializable objects"""

    def default(self, obj: Any) -> Any:
        if hasattr(obj, "model_dump"):
            return self._encode_value(obj.model_dump())

        if hasattr(obj, "__dataclass_fields__"):
            return self._encode_value({k: getattr(obj, k) for k in obj.__dataclass_fields__})

        if hasattr(obj, "to_dict"):
            return self._encode_value(obj.to_dict())

        if hasattr(obj, "asdict"):
            return self._encode_value(obj.asdict())

        if hasattr(obj, "__dict__"):
            return {
                "type": obj.__class__.__name__,
                "attributes": {
                    k: self._encode_value(v)
                    for k, v in obj.__dict__.items()
                    if not k.startswith("_") and not callable(v)
                },
            }

        try:
            return str(obj)
        except Exception:
            return f"<non-serializable-{obj.__class__.__name__}>"

    def _encode_value(self, value: Any) -> Any:
        """Helper method to encode nested values recursively"""
        if isinstance(value, (str, int, float, bool, type(None))):
            return value

        if isinstance(value, (list, tuple)):
            return [self._encode_value(item) for item in value]

        if isinstance(value, dict):
            return {self._encode_value(k): self._encode_value(v) for k, v in value.items()}

        # For any other type, try to encode it using our custom logic
        return self.default(value)

    @classmethod
    def dumps(cls, obj: Any, **kwargs: Any) -> str:
        """Helper method to properly encode objects to JSON string"""
        return json.dumps(obj, cls=cls, **kwargs)
