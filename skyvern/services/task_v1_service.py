import hashlib
import asyncio
import json
import uuid
from typing import Any

import structlog
from fastapi import BackgroundTasks, HTTPException, Request
from sqlalchemy.exc import OperationalError
from onepassword.client import Client as OnePasswordClient

from skyvern.config import settings
from skyvern.exceptions import (
    BitwardenBaseError,
    CredentialParameterNotFoundError,
    OrganizationNotFound,
    SkyvernHTTPException,
    SkyvernException,
    TaskNotFound
)
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.exceptions import LLMProviderError
from skyvern.forge.sdk.core.hashing import generate_url_hash
from skyvern.forge.sdk.executor.factory import AsyncExecutorFactory
from skyvern.forge.sdk.schemas.credentials import PasswordCredential
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.task_generations import TaskGeneration, TaskGenerationBase
from skyvern.forge.sdk.schemas.tasks import Task, TaskRequest, TaskResponse, TaskStatus, SkyvernCredentialConfig, BitwardenCredentialConfig, OnePasswordCredentialConfig, TaskCredentialConfig
from skyvern.forge.sdk.services.bitwarden import BitwardenConstants, BitwardenService
from skyvern.forge.sdk.services.credentials import OnePasswordConstants
from skyvern.schemas.runs import CUA_ENGINES, CUA_RUN_TYPES, RunEngine, RunType
from skyvern.forge.sdk.api.aws import AsyncAWSClient

LOG = structlog.get_logger()


async def generate_task(user_prompt: str, organization: Organization) -> TaskGeneration:
    hash_object = hashlib.sha256()
    hash_object.update(user_prompt.encode("utf-8"))
    user_prompt_hash = hash_object.hexdigest()
    # check if there's a same user_prompt within the past x Hours
    # in the future, we can use vector db to fetch similar prompts
    existing_task_generation = await app.DATABASE.get_task_generation_by_prompt_hash(
        user_prompt_hash=user_prompt_hash, query_window_hours=settings.PROMPT_CACHE_WINDOW_HOURS
    )
    if existing_task_generation:
        new_task_generation = await app.DATABASE.create_task_generation(
            organization_id=organization.organization_id,
            user_prompt=user_prompt,
            user_prompt_hash=user_prompt_hash,
            url=existing_task_generation.url,
            navigation_goal=existing_task_generation.navigation_goal,
            navigation_payload=existing_task_generation.navigation_payload,
            data_extraction_goal=existing_task_generation.data_extraction_goal,
            extracted_information_schema=existing_task_generation.extracted_information_schema,
            llm=existing_task_generation.llm,
            llm_prompt=existing_task_generation.llm_prompt,
            llm_response=existing_task_generation.llm_response,
            source_task_generation_id=existing_task_generation.task_generation_id,
        )
        return new_task_generation

    llm_prompt = prompt_engine.load_prompt("generate-task", user_prompt=user_prompt)
    try:
        llm_response = await app.LLM_API_HANDLER(prompt=llm_prompt, prompt_name="generate-task")
        parsed_task_generation_obj = TaskGenerationBase.model_validate(llm_response)

        # generate a TaskGenerationModel
        task_generation = await app.DATABASE.create_task_generation(
            organization_id=organization.organization_id,
            user_prompt=user_prompt,
            user_prompt_hash=user_prompt_hash,
            url=parsed_task_generation_obj.url,
            navigation_goal=parsed_task_generation_obj.navigation_goal,
            navigation_payload=parsed_task_generation_obj.navigation_payload,
            data_extraction_goal=parsed_task_generation_obj.data_extraction_goal,
            extracted_information_schema=parsed_task_generation_obj.extracted_information_schema,
            suggested_title=parsed_task_generation_obj.suggested_title,
            llm=settings.LLM_KEY,
            llm_prompt=llm_prompt,
            llm_response=str(llm_response),
        )
        return task_generation
    except LLMProviderError:
        LOG.error("Failed to generate task", exc_info=True)
        raise HTTPException(status_code=400, detail="Failed to generate task. Please try again later.")
    except OperationalError:
        LOG.error("Database error when generating task", exc_info=True, user_prompt=user_prompt)
        raise HTTPException(status_code=500, detail="Failed to generate task. Please try again later.")


async def run_task(
    task: TaskRequest,
    organization: Organization,
    engine: RunEngine = RunEngine.skyvern_v1,
    x_max_steps_override: int | None = None,
    x_api_key: str | None = None,
    request: Request | None = None,
    background_tasks: BackgroundTasks | None = None,
) -> Task:
    # Process credentials if provided
    credential_context = {}
    if task.credentials:
        aws_client = AsyncAWSClient()
        credential_handler = TaskCredentialHandler(organization, aws_client)
        try:
            credential_values = await credential_handler.process_credentials(task.credentials)
            credential_context = credential_handler.get_credential_context_for_llm()
            LOG.info(f"Processed {len(credential_values)} credentials for task", credential_keys=list(credential_values.keys()))
        except Exception as e:
            LOG.error(f"Failed to process task credentials: {e}")
            # Continue with task creation even if credential processing fails
            # This allows tasks to run without credentials if needed
    
    created_task = await app.agent.create_task(task, organization.organization_id)
    
    # Store credential context in task metadata if credentials were processed
    if credential_context:
        # Add credential context to the task's navigation payload or store separately
        # This will be available to the LLM during task execution
        existing_payload = created_task.navigation_payload or {}
        if isinstance(existing_payload, dict):
            existing_payload.update(credential_context)
        else:
            # If payload is string or list, create new dict with credentials
            credential_payload = {"credentials_context": credential_context}
            if existing_payload:
                credential_payload["original_payload"] = existing_payload
            existing_payload = credential_payload
        
        # Update task with credential context
        await app.DATABASE.update_task(
            task_id=created_task.task_id,
            organization_id=organization.organization_id,
            navigation_payload=existing_payload,
        )
    
    url_hash = generate_url_hash(task.url)
    run_type = RunType.task_v1
    if engine == RunEngine.openai_cua:
        run_type = RunType.openai_cua
    elif engine == RunEngine.anthropic_cua:
        run_type = RunType.anthropic_cua
    elif engine == RunEngine.ui_tars:
        run_type = RunType.ui_tars
    await app.DATABASE.create_task_run(
        task_run_type=run_type,
        organization_id=organization.organization_id,
        run_id=created_task.task_id,
        title=task.title,
        url=task.url,
        url_hash=url_hash,
    )
    if x_max_steps_override:
        LOG.info(
            "Overriding max steps per run",
            max_steps_override=x_max_steps_override,
            organization_id=organization.organization_id,
            task_id=created_task.task_id,
        )
    await AsyncExecutorFactory.get_executor().execute_task(
        request=request,
        background_tasks=background_tasks,
        task_id=created_task.task_id,
        organization_id=organization.organization_id,
        max_steps_override=x_max_steps_override,
        browser_session_id=task.browser_session_id,
        api_key=x_api_key,
    )
    return created_task


async def get_task_v1_response(task_id: str, organization_id: str | None = None) -> TaskResponse:
    task_obj = await app.DATABASE.get_task(task_id, organization_id=organization_id)
    if not task_obj:
        raise TaskNotFound(task_id=task_id)

    # get latest step
    latest_step = await app.DATABASE.get_latest_step(task_id, organization_id=organization_id)
    if not latest_step:
        return await app.agent.build_task_response(task=task_obj)

    failure_reason: str | None = None
    if task_obj.status == TaskStatus.failed and (latest_step.output or task_obj.failure_reason):
        failure_reason = ""
        if task_obj.failure_reason:
            failure_reason += task_obj.failure_reason or ""
        if latest_step.output is not None and latest_step.output.actions_and_results is not None:
            action_results_string: list[str] = []
            for action, results in latest_step.output.actions_and_results:
                if len(results) == 0:
                    continue
                if results[-1].success:
                    continue
                action_results_string.append(f"{action.action_type} action failed.")

            if len(action_results_string) > 0:
                failure_reason += "(Exceptions: " + str(action_results_string) + ")"
    return await app.agent.build_task_response(
        task=task_obj, last_step=latest_step, failure_reason=failure_reason, need_browser_log=True
    )


async def is_cua_task(
    *,
    task: Task,
) -> bool:
    """Return True if the run, engine, or task indicates a CUA task."""

    if task.workflow_run_id:
        # it's a task based block, should look up the block run to see if it's a CUA task
        block = await app.DATABASE.get_workflow_run_block_by_task_id(
            task_id=task.task_id,
            organization_id=task.organization_id,
        )
        if block.engine is not None and block.engine in CUA_ENGINES:
            return True

    run = await app.DATABASE.get_run(
        run_id=task.task_id,
        organization_id=task.organization_id,
    )
    if run and run.task_run_type in CUA_RUN_TYPES:
        return True

    return False


class TaskCredentialHandler:
    """Handles credential processing for tasks similar to workflow credential handling"""
    
    def __init__(self, organization: Organization, aws_client: AsyncAWSClient):
        self.organization = organization
        self.aws_client = aws_client
        self.credential_values: dict[str, dict[str, str]] = {}
        self.secrets: dict[str, str] = {}
    
    def generate_random_secret_id(self) -> str:
        """Generate a random secret ID"""
        return str(uuid.uuid4()).replace("-", "")
    
    async def process_credentials(self, credentials: list[TaskCredentialConfig]) -> dict[str, dict[str, str]]:
        """Process task credentials and return credential values for LLM context"""
        for credential in credentials:
            if isinstance(credential, SkyvernCredentialConfig):
                await self._process_skyvern_credential(credential)
            elif isinstance(credential, BitwardenCredentialConfig):
                await self._process_bitwarden_credential(credential)
            elif isinstance(credential, OnePasswordCredentialConfig):
                await self._process_onepassword_credential(credential)
        
        return self.credential_values
    
    async def _process_skyvern_credential(self, credential: SkyvernCredentialConfig) -> None:
        """Process Skyvern credential"""
        LOG.info(f"Processing Skyvern credential: {credential.credential_id}")
        
        db_credential = await app.DATABASE.get_credential(
            credential.credential_id, 
            organization_id=self.organization.organization_id
        )
        if db_credential is None:
            raise CredentialParameterNotFoundError(credential.credential_id)

        bitwarden_credential = await BitwardenService.get_credential_item(db_credential.item_id)
        credential_item = bitwarden_credential.credential

        self.credential_values[credential.key] = {}
        credential_dict = credential_item.model_dump()
        
        for key, value in credential_dict.items():
            if value is not None:
                random_secret_id = self.generate_random_secret_id()
                secret_id = f"{random_secret_id}_{key}"
                self.secrets[secret_id] = value
                self.credential_values[credential.key][key] = secret_id

        # Handle TOTP if available
        if isinstance(credential_item, PasswordCredential) and credential_item.totp is not None:
            random_secret_id = self.generate_random_secret_id()
            totp_secret_id = f"{random_secret_id}_totp"
            self.secrets[totp_secret_id] = BitwardenConstants.TOTP
            totp_secret_value = f"{totp_secret_id}_value"
            self.secrets[totp_secret_value] = credential_item.totp
            self.credential_values[credential.key]["totp"] = totp_secret_id

    async def _process_bitwarden_credential(self, credential: BitwardenCredentialConfig) -> None:
        """Process Bitwarden credential"""
        LOG.info(f"Processing Bitwarden credential: {credential.key}")
        
        try:
            # Get the Bitwarden login credentials from AWS secrets
            client_id = settings.BITWARDEN_CLIENT_ID or await self.aws_client.get_secret(
                credential.bitwarden_client_id_aws_secret_key
            )
            client_secret = settings.BITWARDEN_CLIENT_SECRET or await self.aws_client.get_secret(
                credential.bitwarden_client_secret_aws_secret_key
            )
            master_password = settings.BITWARDEN_MASTER_PASSWORD or await self.aws_client.get_secret(
                credential.bitwarden_master_password_aws_secret_key
            )
        except Exception as e:
            LOG.error(f"Failed to get Bitwarden login credentials from AWS secrets. Error: {e}")
            raise e

        if not client_id or not client_secret or not master_password:
            raise ValueError("Bitwarden credentials not found in AWS secrets")

        try:
            secret_credentials = await BitwardenService.get_secret_value_from_url(
                client_id,
                client_secret,
                master_password,
                self.organization.bw_organization_id,
                self.organization.bw_collection_ids,
                credential.url_parameter_key,
                collection_id=credential.bitwarden_collection_id,
                item_id=credential.bitwarden_item_id,
            )
            
            if secret_credentials:
                random_secret_id = self.generate_random_secret_id()
                # username secret
                username_secret_id = f"{random_secret_id}_username"
                self.secrets[username_secret_id] = secret_credentials[BitwardenConstants.USERNAME]
                # password secret
                password_secret_id = f"{random_secret_id}_password"
                self.secrets[password_secret_id] = secret_credentials[BitwardenConstants.PASSWORD]
                
                self.credential_values[credential.key] = {
                    "username": username_secret_id,
                    "password": password_secret_id,
                }

                if BitwardenConstants.TOTP in secret_credentials and secret_credentials[BitwardenConstants.TOTP]:
                    totp_secret_id = f"{random_secret_id}_totp"
                    self.secrets[totp_secret_id] = BitwardenConstants.TOTP
                    totp_secret_value = f"{totp_secret_id}_value"
                    self.secrets[totp_secret_value] = secret_credentials[BitwardenConstants.TOTP]
                    self.credential_values[credential.key]["totp"] = totp_secret_id
        except Exception as e:
            LOG.error(f"Failed to get Bitwarden credentials. Error: {e}")
            raise e

    async def _process_onepassword_credential(self, credential: OnePasswordCredentialConfig) -> None:
        """Process 1Password credential"""
        LOG.info(f"Processing 1Password credential: {credential.key}")
        
        try:
            op_service_account_token = settings.ONEPASSWORD_SERVICE_ACCOUNT_TOKEN or await self.aws_client.get_secret(
                OnePasswordConstants.ONEPASSWORD_SERVICE_ACCOUNT_TOKEN
            )
            
            if not op_service_account_token:
                raise ValueError("1Password service account token not found")

            client = OnePasswordClient(
                auth=op_service_account_token,
                integration_name="skyvern",
                integration_version="v1.0.0",
            )
            
            item = client.items.get(vault=credential.vault_id, item_id=credential.item_id)
            
            random_secret_id = self.generate_random_secret_id()
            self.credential_values[credential.key] = {}
            
            # Extract username and password from 1Password item
            for field in item.fields:
                if field.reference:
                    field_name = field.reference.lower()
                    if "username" in field_name or "email" in field_name:
                        username_secret_id = f"{random_secret_id}_username"
                        self.secrets[username_secret_id] = field.value
                        self.credential_values[credential.key]["username"] = username_secret_id
                    elif "password" in field_name:
                        password_secret_id = f"{random_secret_id}_password"
                        self.secrets[password_secret_id] = field.value
                        self.credential_values[credential.key]["password"] = password_secret_id
        except Exception as e:
            LOG.error(f"Failed to get 1Password credentials. Error: {e}")
            raise e

    def get_credential_context_for_llm(self) -> dict[str, Any]:
        """Generate credential context for LLM"""
        if not self.credential_values:
            return {}
        
        context = {
            "credentials": {
                "available": list(self.credential_values.keys()),
                "details": {}
            }
        }
        
        for cred_key, cred_data in self.credential_values.items():
            context["credentials"]["details"][cred_key] = {
                "fields": list(cred_data.keys()),
                "type": "credential"
            }
        
        return context
