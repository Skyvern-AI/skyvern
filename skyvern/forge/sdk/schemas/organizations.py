from pydantic import BaseModel


class OrganizationUpdate(BaseModel):
    organization_name: str | None = None
    webhook_callback_url: str | None = None
    max_steps_per_run: int | None = None
    max_retries_per_step: int | None = None
