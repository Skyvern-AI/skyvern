from typing import Any

from pydantic import BaseModel

from skyvern.core.script_generations.script_skyvern_page import ScriptSkyvernPage, script_run_context_manager
from skyvern.core.script_generations.skyvern_page import RunContext, SkyvernPage
from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameterType


async def setup(
    parameters: dict[str, Any],
    generated_parameter_cls: type[BaseModel] | None = None,
    browser_session_id: str | None = None,
) -> tuple[SkyvernPage, RunContext]:
    # transform any secrets/credential parameters. For example, if there's only one credential in the parameters: {"cred_12345": "cred_12345"},
    # it should be transformed to {"cred_12345": {"username": "secret_5fBoa_username", "password": "secret_5fBoa_password"}}
    # context comes from app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
    context = skyvern_context.current()
    if context and context.organization_id and context.workflow_run_id:
        browser_session_id = browser_session_id or context.browser_session_id
        workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(context.workflow_run_id)
        parameters_in_workflow_context = workflow_run_context.parameters
        for key in parameters:
            if key in parameters_in_workflow_context:
                parameter = parameters_in_workflow_context[key]
                if parameter.workflow_parameter_type == WorkflowParameterType.CREDENTIAL_ID:
                    parameters[key] = workflow_run_context.values[key]
        context.script_run_parameters.update(parameters)
    skyvern_page = await ScriptSkyvernPage.create(browser_session_id=browser_session_id)
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
