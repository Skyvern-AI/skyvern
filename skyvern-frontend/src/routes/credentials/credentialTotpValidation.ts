const AUTHENTICATOR_KEY_REQUIRED_MESSAGE = "Authenticator key is required.";
const AUTHENTICATOR_KEY_FORMAT_MESSAGE =
  "Authenticator key should be a raw Base32 setup key or full otpauth:// URI.";
const MIN_AUTHENTICATOR_KEY_LENGTH = 16;

type AuthenticatorKeyValues = {
  totp: string;
  totp_type: "authenticator" | "email" | "text" | "none";
};

function getAuthenticatorKeyError(
  values: AuthenticatorKeyValues,
): string | null {
  if (values.totp_type !== "authenticator") {
    return null;
  }

  const raw = values.totp.trim();
  if (raw === "") {
    return AUTHENTICATOR_KEY_REQUIRED_MESSAGE;
  }
  if (
    raw.toLowerCase().startsWith("otpauth://") ||
    /(?:^|[?&])secret=/i.test(raw)
  ) {
    return null;
  }

  const normalized = raw.replace(/[\s-]/g, "").toUpperCase();
  if (
    normalized.length < MIN_AUTHENTICATOR_KEY_LENGTH ||
    !/^[A-Z2-7]+=*$/.test(normalized)
  ) {
    return AUTHENTICATOR_KEY_FORMAT_MESSAGE;
  }
  return null;
}

export { getAuthenticatorKeyError };
