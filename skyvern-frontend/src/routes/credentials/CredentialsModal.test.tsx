// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  cleanup,
} from "@testing-library/react";
import { AxiosError, AxiosHeaders } from "axios";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { getAuthenticatorKeyError } from "./credentialTotpValidation";
import { CredentialsModal } from "./CredentialsModal";
import { CredentialModalTypes } from "./useCredentialModalState";

const postMock = vi.hoisted(() => vi.fn());
const toastMock = vi.hoisted(() => vi.fn());

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn(async () => ({
    post: postMock,
    patch: vi.fn(),
    delete: vi.fn(),
  })),
}));

vi.mock("@/components/ui/use-toast", () => ({
  toast: toastMock,
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

vi.mock("@/hooks/useCustomCredentialServiceConfig", () => ({
  useCustomCredentialServiceConfig: () => ({ parsedConfig: null }),
}));

vi.mock("@/routes/workflows/hooks/useCredentialsQuery", () => ({
  useCredentialsQuery: () => ({ data: [] }),
}));

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

function renderPasswordCredentialsModal() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <CredentialsModal
          isOpen
          onOpenChange={vi.fn()}
          overrideType={CredentialModalTypes.PASSWORD}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

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

describe("CredentialsModal authenticator save errors", () => {
  async function fillAndSubmitAuthenticatorCredential(totp: string) {
    renderPasswordCredentialsModal();

    await waitFor(() => {
      expect(screen.getByDisplayValue("credentials")).toBeTruthy();
    });
    const usernameInput = Array.from(
      document.querySelectorAll<HTMLInputElement>("input"),
    ).find(
      (input) =>
        input.type === "text" && input.value === "" && input.placeholder === "",
    );
    expect(usernameInput).toBeTruthy();
    fireEvent.change(usernameInput as HTMLInputElement, {
      target: { value: "user@example.com" },
    });
    const passwordInput = document.querySelector('input[type="password"]');
    expect(passwordInput).toBeTruthy();
    fireEvent.change(passwordInput as HTMLInputElement, {
      target: { value: "password" },
    });

    fireEvent.click(screen.getByText("Two-Factor Authentication"));
    const authenticatorInput = screen.getByPlaceholderText(
      "e.g. JBSWY3DPEHPK3PXP",
    );
    fireEvent.change(authenticatorInput, {
      target: { value: totp },
    });

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    return authenticatorInput as HTMLInputElement;
  }

  it("shows a rejected authenticator QR inline and keeps the submitted value in the field", async () => {
    postMock.mockRejectedValueOnce(
      axiosErrorWithDetail({
        error_code: "authenticator_no_code_secret",
        message: "This QR code enrolls push approval.",
        vendor: "microsoft",
      }),
    );

    const decodedQrPayload = "phonefactor://activate_account?code=123456";
    const authenticatorInput =
      await fillAndSubmitAuthenticatorCredential(decodedQrPayload);

    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith(
        "/credentials",
        expect.objectContaining({
          credential: expect.objectContaining({
            totp: decodedQrPayload,
            totp_type: "authenticator",
          }),
        }),
      );
    });
    await waitFor(() => {
      expect(screen.getByText(/push-approval app/)).toBeTruthy();
    });
    expect((authenticatorInput as HTMLInputElement).value).toBe(
      decodedQrPayload,
    );
  }, 10_000);

  it("shows enterprise-required feedback inline without a destructive toast", async () => {
    postMock.mockRejectedValueOnce(
      axiosErrorWithDetail({
        error_code: "authenticator_feature_restricted",
        message: "Enterprise plan required.",
        vendor: "okta",
      }),
    );

    await fillAndSubmitAuthenticatorCredential(
      '{"methods":[{"type":"totp","sharedSecret":"JBSWY3DPEHPK3PXP"}]}',
    );

    await waitFor(() => {
      expect(screen.getByText(/Skyvern enterprise plan/)).toBeTruthy();
    });
    expect(toastMock).not.toHaveBeenCalled();
  }, 10_000);
});
