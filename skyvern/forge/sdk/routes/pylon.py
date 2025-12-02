import hashlib
import hmac

import structlog
from fastapi import Query

from skyvern.config import settings
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.pylon import PylonHash

LOG = structlog.get_logger()


@base_router.get(
    "/pylon/email_hash",
    include_in_schema=False,
    response_model=PylonHash,
)
def get_pylon_email_hash(email: str = Query(...)) -> PylonHash:
    no_hash = "???-no-hash-???"
    secret = settings.PYLON_IDENTITY_VERIFICATION_SECRET

    if not secret:
        LOG.warning("No Pylon identity verification secret", email=email)
        return PylonHash(hash=no_hash)

    try:
        secret_bytes = bytes.fromhex(secret)
        signature = hmac.new(secret_bytes, email.encode(), hashlib.sha256).hexdigest()

        return PylonHash(hash=signature)
    except Exception:
        LOG.exception("Failed to generate Pylon email hash", email=email)
        return PylonHash(hash=no_hash)
