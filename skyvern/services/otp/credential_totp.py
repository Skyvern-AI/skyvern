import pyotp
import structlog

from skyvern.forge import app
from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.services.otp.models import OTPValue

LOG = structlog.get_logger()


def try_generate_totp_from_credential(workflow_run_id: str | None) -> OTPValue | None:
    """Try to generate a TOTP code from a credential secret stored in workflow run context.

    Scans workflow_run_context.values for credential entries with a "totp" key
    (e.g. Bitwarden, 1Password, Azure Key Vault credentials) and generates a
    TOTP code using pyotp. This should be checked BEFORE poll_otp_value so that
    credential-based TOTP takes priority over webhook (totp_url) and totp_identifier.
    """
    if not workflow_run_id:
        return None

    workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
    if not workflow_run_context:
        return None

    for key, value in workflow_run_context.values.items():
        if isinstance(value, dict) and "totp" in value:
            totp_secret_id = value.get("totp")
            if not totp_secret_id or not isinstance(totp_secret_id, str):
                continue
            totp_secret_key = workflow_run_context.totp_secret_value_key(totp_secret_id)
            totp_secret = workflow_run_context.get_original_secret_value_or_none(totp_secret_key)
            if totp_secret:
                try:
                    code = pyotp.TOTP(totp_secret).now()
                    LOG.info(
                        "Generated TOTP from credential secret",
                        workflow_run_id=workflow_run_id,
                        credential_key=key,
                    )
                    return OTPValue(value=code, type=OTPType.TOTP)
                except Exception:
                    LOG.warning(
                        "Failed to generate TOTP from credential secret",
                        workflow_run_id=workflow_run_id,
                        credential_key=key,
                        exc_info=True,
                    )
    return None
