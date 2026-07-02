// @vitest-environment jsdom

import { AxiosError, AxiosHeaders } from "axios";
import { describe, expect, it } from "vitest";

import {
  getAuthenticatorSaveError,
  getCredentialErrorMessage,
} from "./authenticatorSaveError";

function axiosErrorWithDetail(detail: unknown): AxiosError {
  const error = new AxiosError("Request failed");
  error.response = {
    data: { detail },
    status: 400,
    statusText: "Bad Request",
    headers: {},
    config: { headers: new AxiosHeaders() },
  };
  return error;
}

describe("getAuthenticatorSaveError", () => {
  it("maps the structured no-code-secret code to actionable code-based setup copy", () => {
    const result = getAuthenticatorSaveError(
      axiosErrorWithDetail({
        error_code: "authenticator_no_code_secret",
        message: "Push enrollment payloads are not supported.",
      }),
    );

    expect(result?.code).toBe("no_code_secret");
    expect(result?.message).toMatch(/code-based setup key/i);
    expect(result?.message).toMatch(/push-approval/i);
    expect(result?.message).toMatch(/authenticator app or one-time code/i);
  });

  it("maps the structured enterprise-required code to enterprise access copy", () => {
    const result = getAuthenticatorSaveError(
      axiosErrorWithDetail({
        error_code: "authenticator_feature_restricted",
        message: "Enterprise plan required.",
      }),
    );

    expect(result?.code).toBe("enterprise_required");
    expect(result?.message).toMatch(/enterprise plan/i);
  });

  it("preserves the detected vendor id for enterprise-required copy", () => {
    const result = getAuthenticatorSaveError(
      axiosErrorWithDetail({
        error_code: "authenticator_feature_restricted",
        message: "Enterprise plan required.",
        vendor: "example",
      }),
    );

    expect(result?.code).toBe("enterprise_required");
    expect(result?.message).toBe(
      "This authenticator requires a Skyvern enterprise plan.",
    );
    expect(result?.vendor).toBe("example");
  });

  it("maps unsupported TOTP config to the backend message", () => {
    const result = getAuthenticatorSaveError(
      axiosErrorWithDetail({
        error_code: "authenticator_totp_config_unsupported",
        message: "The authenticator setup code is malformed.",
      }),
    );

    expect(result?.code).toBe("unsupported_totp_config");
    expect(result?.message).toBe("The authenticator setup code is malformed.");
  });

  it("falls back to the backend message for an invalid-key code", () => {
    const result = getAuthenticatorSaveError(
      axiosErrorWithDetail({
        error_code: "invalid_authenticator_key",
        message: "That is not a valid Base32 secret.",
      }),
    );

    expect(result?.code).toBe("invalid_authenticator_key");
    expect(result?.message).toBe("That is not a valid Base32 secret.");
  });

  it("does not infer unsupported push enrollment from unknown future codes", () => {
    const result = getAuthenticatorSaveError(
      axiosErrorWithDetail({
        error_code: "batch_enrollment_failed",
        message: "The authenticator setup could not be saved.",
      }),
    );

    expect(result?.code).toBe("unknown");
    expect(result?.message).toBe("The authenticator setup could not be saved.");
  });

  it("classifies a legacy string detail that mentions the authenticator key", () => {
    const result = getAuthenticatorSaveError(
      axiosErrorWithDetail(
        "Invalid authenticator key. Paste the raw Base32 setup key.",
      ),
    );

    expect(result?.code).toBe("invalid_authenticator_key");
    expect(result?.message).toContain("Invalid authenticator key");
  });

  it("returns null for a legacy string detail unrelated to the authenticator", () => {
    expect(
      getAuthenticatorSaveError(
        axiosErrorWithDetail("Username and password are required"),
      ),
    ).toBeNull();
  });

  it("returns null when there is no response detail", () => {
    expect(getAuthenticatorSaveError(new Error("boom"))).toBeNull();
  });
});

describe("getCredentialErrorMessage", () => {
  it("returns a legacy string detail verbatim", () => {
    expect(
      getCredentialErrorMessage(axiosErrorWithDetail("Something went wrong")),
    ).toBe("Something went wrong");
  });

  it("returns the message field from a structured detail", () => {
    expect(
      getCredentialErrorMessage(
        axiosErrorWithDetail({
          error_code: "enterprise_required",
          message: "Enterprise plan required.",
        }),
      ),
    ).toBe("Enterprise plan required.");
  });

  it("returns null when no usable detail is present", () => {
    expect(getCredentialErrorMessage(new Error("boom"))).toBeNull();
  });
});
