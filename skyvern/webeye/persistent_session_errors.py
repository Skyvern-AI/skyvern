from fastapi import HTTPException

BROWSER_SESSION_CREDIT_ADMISSION_REFUSAL_DETAIL = "Credits exhausted. Upgrade your plan in Billing."


class BrowserSessionCreditAdmissionRefusal(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=402, detail=BROWSER_SESSION_CREDIT_ADMISSION_REFUSAL_DETAIL)
