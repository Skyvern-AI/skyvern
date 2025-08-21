from typing import Any

from pydantic import BaseModel

from skyvern.core.script_generations.script_run_context_manager import script_run_context_manager
from skyvern.core.script_generations.skyvern_page import RunContext, SkyvernPage


async def setup(
    parameters: dict[str, Any], generated_parameter_cls: type[BaseModel] | None = None
) -> tuple[SkyvernPage, RunContext]:
    skyvern_page = await SkyvernPage.create()
    run_context = RunContext(
        parameters=parameters,
        page=skyvern_page,
        # TODO: generate all parameters with llm here - then we can skip generating input text one by one in the fill/type methods
        generated_parameters=generated_parameter_cls().model_dump() if generated_parameter_cls else None,
    )
    script_run_context_manager.set_run_context(run_context)
    return skyvern_page, run_context


# async def transform_parameters(parameters: dict[str, Any] | BaseModel | None = None, generated_parameter_cls: type[BaseModel] | None = None) -> dict[str, Any] | None:
#     if parameters is None:
#         return None

#     if generated_parameter_cls:
#         if isinstance(parameters, dict):
#             # TODO: use llm to generate
#             return generated_parameter_cls.model_validate(parameters)
#         if isinstance(parameters, BaseModel):
#             return parameters
#         return generated_parameter_cls.model_validate(parameters)
#     else:
#         if isinstance(parameters, dict):
#             return parameters
#         if isinstance(parameters, BaseModel):
#             return parameters.model_dump()
#         return parameters
