import { isAxiosError } from "axios";

const PAYMENT_REQUIRED_STATUS = 402;

// A 402 means the organization is out of credits, which is a property of the
// organization rather than of the individual request. Retrying or re-issuing
// the same call can only produce another 402, so every caller must stop.
function isPaymentRequiredError(error: unknown): boolean {
  return (
    isAxiosError(error) && error.response?.status === PAYMENT_REQUIRED_STATUS
  );
}

export { isPaymentRequiredError };
