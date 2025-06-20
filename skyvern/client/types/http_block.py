import typing
from typing import Any, Dict, List, Optional

import typing_extensions
from pydantic import Field

from ..core.universal_base_model import UniversalBaseModel
from .output_parameter import OutputParameter
from .workflow_parameter import WorkflowParameter


class HttpBlock(UniversalBaseModel):
    block_type: typing_extensions.Literal["http_request"] = Field(default="http_request")
    label: str
    output_parameter: OutputParameter
    curl_command: str
    method: Optional[str] = None
    url: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    body: Optional[str] = None
    timeout: int = Field(default=30)
    parameters: List[WorkflowParameter] = []
    continue_on_failure: bool = Field(default=False)
    model: Optional[Dict[str, Any]] = None


if typing:
    import typing_extensions