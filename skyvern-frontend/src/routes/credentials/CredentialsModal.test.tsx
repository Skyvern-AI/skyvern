// @vitest-environment jsdom

import { describe, expect, it } from "vitest";

import { getAuthenticatorKeyError } from "./credentialTotpValidation";

describe("getAuthenticatorKeyError", () => {
  it("requires an authenticator key when authenticator 2FA is selected", () => {
    expect(
      getAuthenticatorKeyError({ totp: " ", totp_type: "authenticator" }),
    ).toBe("Authenticator key is required.");
  });

  it("lets backend validation decide authenticator key format", () => {
    expect(
      getAuthenticatorKeyError({
        totp: "provider-specific-payload",
        totp_type: "authenticator",
      }),
    ).toBeNull();
  });

  it("accepts a raw Base32 key or a full otpauth URI", () => {
    expect(
      getAuthenticatorKeyError({
        totp: "JBSW-Y3DP EHPK3PXP",
        totp_type: "authenticator",
      }),
    ).toBeNull();
    expect(
      getAuthenticatorKeyError({
        totp: "otpauth://totp/user@example.com?secret=JBSWY3DPEHPK3PXP",
        totp_type: "authenticator",
      }),
    ).toBeNull();
  });

  it("does not validate the key for email, text, or disabled 2FA methods", () => {
    expect(
      getAuthenticatorKeyError({ totp: "", totp_type: "email" }),
    ).toBeNull();
    expect(
      getAuthenticatorKeyError({ totp: "", totp_type: "text" }),
    ).toBeNull();
    expect(
      getAuthenticatorKeyError({ totp: "", totp_type: "none" }),
    ).toBeNull();
  });
});
