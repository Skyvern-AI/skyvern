from datetime import datetime

from pydantic import BaseModel, ConfigDict


class OrganizationBitwardenCollection(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    organization_bitwarden_collection_id: str
    organization_id: str
    collection_id: str
    created_at: datetime
    modified_at: datetime
