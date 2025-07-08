from pydantic import BaseModel


class PylonHash(BaseModel):
    hash: str
