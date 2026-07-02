import { isAxiosError } from "axios";

type AuthenticatorErrorCode =
  | "no_code_secret"
  | "unsupported_totp_config"
  | "enterprise_required"
  | "invalid_authenticator_key"
  | "unknown";

type AuthenticatorSaveError = {
  code: AuthenticatorErrorCode;
  message: string;
  vendor?: string;
};

type StructuredErrorDetail = {
  error_code?: unknown;
  message?: unknown;
  vendor?: unknown;
};

const EXACT_ERROR_CODE_MAP: Record<string, AuthenticatorErrorCode> = {
  authenticator_no_code_secret: "no_code_secret",
  authenticator_totp_config_unsupported: "unsupported_totp_config",
  authenticator_feature_restricted: "enterprise_required",
  invalid_authenticator_key: "invalid_authenticator_key",
  authenticator_key_required: "invalid_authenticator_key",
};

const NO_CODE_SECRET_MESSAGE =
  "This QR code doesn't contain a code-based setup key that Skyvern can use. It may enroll a push-approval app or device-bound authenticator. In the site's security settings, choose an authenticator app or one-time code, or use a TOTP setup key instead, then scan that QR code or paste its setup key.";

const INVALID_KEY_MESSAGE =
  "Invalid authenticator key. Paste the raw Base32 setup key or full otpauth:// URI from the website's 2FA setup screen.";
const UNSUPPORTED_TOTP_CONFIG_MESSAGE =
  "We recognized this as an authenticator setup QR, but its code configuration is malformed or unsupported. Use a standard setup key or otpauth:// URI instead.";

function enterpriseMessage(): string {
  return "This authenticator requires a Skyvern enterprise plan.";
}

function classifyCode(rawCode: string): AuthenticatorErrorCode | null {
  const code = rawCode.toLowerCase();
  const exactCode = EXACT_ERROR_CODE_MAP[code];
  if (exactCode) {
    return exactCode;
  }
  return rawCode ? "unknown" : null;
}

function resolveMessage(
  code: AuthenticatorErrorCode,
  backendMessage: string | undefined,
): string {
  switch (code) {
    case "no_code_secret":
      return NO_CODE_SECRET_MESSAGE;
    case "unsupported_totp_config":
      return backendMessage?.trim() || UNSUPPORTED_TOTP_CONFIG_MESSAGE;
    case "enterprise_required":
      return enterpriseMessage();
    case "invalid_authenticator_key":
      return backendMessage?.trim() || INVALID_KEY_MESSAGE;
    case "unknown":
      return backendMessage?.trim() || INVALID_KEY_MESSAGE;
  }
}

function fromStructuredDetail(
  detail: StructuredErrorDetail,
): AuthenticatorSaveError | null {
  const rawCode =
    typeof detail.error_code === "string" ? detail.error_code : "";
  const backendMessage =
    typeof detail.message === "string" ? detail.message : undefined;
  const vendor = typeof detail.vendor === "string" ? detail.vendor : undefined;
  const code = classifyCode(rawCode);
  if (!code) {
    if (backendMessage && /authenticator/i.test(backendMessage)) {
      return { code: "unknown", message: backendMessage.trim(), vendor };
    }
    return null;
  }
  return {
    code,
    message: resolveMessage(code, backendMessage),
    vendor,
  };
}

function fromStringDetail(detail: string): AuthenticatorSaveError | null {
  const trimmed = detail.trim();
  if (!/authenticator key/i.test(trimmed)) {
    return null;
  }
  const code: AuthenticatorErrorCode = /invalid/i.test(trimmed)
    ? "invalid_authenticator_key"
    : "unknown";
  return { code, message: trimmed };
}

function getErrorDetail(error: unknown): unknown {
  if (isAxiosError<{ detail?: unknown }>(error)) {
    return error.response?.data?.detail;
  }
  return undefined;
}

/**
 * Extracts authenticator-specific user-facing error info from Axios failures, including legacy string details and structured `{ error_code, message, vendor? }` details.
 * Returns null when the error is not an authenticator setup failure so callers can fall back to generic toast handling.
 */
function getAuthenticatorSaveError(
  error: unknown,
): AuthenticatorSaveError | null {
  const detail = getErrorDetail(error);
  if (typeof detail === "string") {
    return fromStringDetail(detail);
  }
  if (detail && typeof detail === "object") {
    return fromStructuredDetail(detail as StructuredErrorDetail);
  }
  return null;
}

/**
 * Extracts a plain user-facing string from an Axios error for toast copy,
 * normalizing both the legacy string `detail` and the structured object
 * `detail` shapes. Returns null when no detail is present.
 */
function getCredentialErrorMessage(error: unknown): string | null {
  const detail = getErrorDetail(error);
  if (typeof detail === "string") {
    return detail.trim() || null;
  }
  if (detail && typeof detail === "object") {
    const message = (detail as StructuredErrorDetail).message;
    if (typeof message === "string" && message.trim()) {
      return message.trim();
    }
  }
  return null;
}

export { getAuthenticatorSaveError, getCredentialErrorMessage };
export type { AuthenticatorSaveError, AuthenticatorErrorCode };
