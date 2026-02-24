from dataclasses import dataclass

from pydantic import BaseModel, Field

from skyvern.forge.sdk.schemas.totp_codes import OTPType

MFANavigationPayload = dict | list | str | None


@dataclass(slots=True)
class OTPPollContext:
    organization_id: str
    task_id: str | None = None
    workflow_id: str | None = None
    workflow_run_id: str | None = None
    workflow_permanent_id: str | None = None
    totp_verification_url: str | None = None
    totp_identifier: str | None = None

    @property
    def needs_manual_input(self) -> bool:
        return not self.totp_verification_url


class OTPValue(BaseModel):
    value: str = Field(..., description="The value of the OTP code.")
    type: OTPType | None = Field(None, description="The type of the OTP code.")

    def get_otp_type(self) -> OTPType:
        if self.type:
            return self.type
        value = self.value.strip().lower()
        if value.startswith("https://") or value.startswith("http://"):
            return OTPType.MAGIC_LINK
        return OTPType.TOTP


class OTPResultParsedByLLM(BaseModel):
    reasoning: str = Field(..., description="The reasoning of the OTP code.")
    otp_type: OTPType | None = Field(None, description="The type of the OTP code.")
    otp_value_found: bool = Field(..., description="Whether the OTP value is found.")
    otp_value: str | None = Field(None, description="The OTP value.")
