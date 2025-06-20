import typing
from typing import Any, Dict, List, Optional

import typing_extensions
from pydantic import Field

from ..core.universal_base_model import UniversalBaseModel


class HttpBlockYaml(UniversalBaseModel):
    block_type: typing_extensions.Literal["http_request"] = Field(default="http_request")
    label: str
    curl_command: str
    method: Optional[str] = None
    url: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    body: Optional[str] = None
    timeout: int = Field(default=30)
    parameter_keys: Optional[List[str]] = None
    continue_on_failure: bool = Field(default=False)
    model: Optional[Dict[str, Any]] = None