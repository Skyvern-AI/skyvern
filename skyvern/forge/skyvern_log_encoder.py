import json
from typing import Any

class SkyvernLogEncoder(json.JSONEncoder):
    """Custom JSON encoder for Skyvern logs that handles non-serializable objects"""
    def default(self, obj: Any) -> Any:
        if hasattr(obj, 'model_dump'):
            return obj.model_dump()

        if hasattr(obj, '__dataclass_fields__'):
            return {k: getattr(obj, k) for k in obj.__dataclass_fields__}

        if hasattr(obj, '__dict__'):
            return {
                'type': obj.__class__.__name__,
                'attributes': {k: v for k, v in obj.__dict__.items() if not k.startswith('_')}
            }
        # Handle other non-serializable objects
        try:
            return str(obj)
        except Exception:
            return f"<non-serializable-{obj.__class__.__name__}>"
