from pydantic import BaseModel

from skyvern.forge.sdk.models import Organization, OrganizationAuthToken


class GetOrganizationsResponse(BaseModel):
    organizations: list[Organization]


class GetOrganizationAPIKeysResponse(BaseModel):
    api_keys: list[OrganizationAuthToken]


class OrganizationUpdate(BaseModel):
    max_steps_per_run: int | None = None
