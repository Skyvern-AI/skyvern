import structlog

from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.services.otp.models import OTPResultParsedByLLM, OTPValue

LOG = structlog.get_logger()


async def parse_otp_login(
    content: str,
    organization_id: str,
    enforced_otp_type: OTPType | None = None,
) -> OTPValue | None:
    prompt = prompt_engine.load_prompt(
        "parse-otp-login",
        content=content,
        enforced_otp_type=enforced_otp_type.value if enforced_otp_type else None,
    )
    resp = await app.SECONDARY_LLM_API_HANDLER(
        prompt=prompt, prompt_name="parse-otp-login", organization_id=organization_id
    )
    LOG.info("OTP Login Parser Response", resp=resp, enforced_otp_type=enforced_otp_type)
    otp_result = OTPResultParsedByLLM.model_validate(resp)
    if otp_result.otp_value_found and otp_result.otp_value:
        return OTPValue(value=otp_result.otp_value, type=otp_result.otp_type)
    return None
