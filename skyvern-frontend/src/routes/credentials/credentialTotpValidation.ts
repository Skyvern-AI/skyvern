const AUTHENTICATOR_KEY_REQUIRED_MESSAGE = "Authenticator key is required.";

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
  return null;
}

export { getAuthenticatorKeyError };
