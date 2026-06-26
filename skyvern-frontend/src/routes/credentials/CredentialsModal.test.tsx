// @vitest-environment jsdom

import { describe, expect, it } from "vitest";

import { getAuthenticatorKeyError } from "./credentialTotpValidation";

describe("getAuthenticatorKeyError", () => {
  it("requires an authenticator key when authenticator 2FA is selected", () => {
    expect(
      getAuthenticatorKeyError({ totp: " ", totp_type: "authenticator" }),
    ).toBe("Authenticator key is required.");
  });

  it("rejects Base32-shaped authenticator keys that are too short to be useful", () => {
    expect(
      getAuthenticatorKeyError({ totp: "A", totp_type: "authenticator" }),
    ).toBe(
      "Authenticator key should be a raw Base32 setup key or full otpauth:// URI.",
    );
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
