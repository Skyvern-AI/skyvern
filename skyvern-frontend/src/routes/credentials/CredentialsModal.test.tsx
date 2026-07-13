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

import type { CredentialApiResponse } from "@/api/types";

import { getAuthenticatorKeyError } from "./credentialTotpValidation";
import { CredentialsModal } from "./CredentialsModal";
import { CredentialModalTypes } from "./useCredentialModalState";

const postMock = vi.hoisted(() => vi.fn());
const patchMock = vi.hoisted(() => vi.fn());
const deleteMock = vi.hoisted(() => vi.fn());
const getMock = vi.hoisted(() => vi.fn());
const toastMock = vi.hoisted(() => vi.fn());

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn(async () => ({
    post: postMock,
    patch: patchMock,
    delete: deleteMock,
    get: getMock,
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

const editingPasswordCredential: CredentialApiResponse = {
  credential_id: "real-cred-id",
  credential_type: "password",
  name: "Acme Login",
  credential: {
    username: "user@example.com",
    totp_type: "none",
    totp_identifier: null,
  },
  browser_profile_id: "existing-profile-id",
  tested_url: "https://example.com/login",
  user_context: null,
  save_browser_session_intent: true,
  folder_id: null,
  proxy_location: null,
  proxy_session_id: null,
};

function renderEditPasswordCredentialsModal() {
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
          editingCredential={editingPasswordCredential}
          onStartBackgroundTest={vi.fn()}
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

describe("CredentialsModal edit-mode inline test", () => {
  it("updates the real credential and deletes the temp one instead of renaming the temp credential in place, even if 'save browser session' gets unchecked before saving", async () => {
    // 1st POST: startTest's /credentials/test-login. 2nd POST: the real
    // credential's /credentials/{id}/update once Save is clicked.
    postMock
      .mockResolvedValueOnce({
        data: { credential_id: "temp-cred-id", workflow_run_id: "wr-1" },
      })
      .mockResolvedValueOnce({
        data: { credential_id: "real-cred-id", name: "Acme Login" },
      });
    getMock.mockResolvedValueOnce({
      data: {
        status: "completed",
        browser_profile_id: "new-profile-id",
        tested_url: "https://example.com/login",
      },
    });
    patchMock.mockResolvedValue({ data: {} });
    deleteMock.mockResolvedValue({ data: {} });

    renderEditPasswordCredentialsModal();

    fireEvent.click(screen.getAllByLabelText("Edit credential values")[0]!);
    const passwordInput = document.querySelector('input[type="password"]');
    expect(passwordInput).toBeTruthy();
    fireEvent.change(passwordInput as HTMLInputElement, {
      target: { value: "rotated-password" },
    });

    fireEvent.click(screen.getByRole("button", { name: "Test" }));

    // Real 3s poll delay inside the component — wait for the button label
    // to flip once testStatus reaches "completed".
    await waitFor(
      () => {
        expect(screen.getByRole("button", { name: "Retest" })).toBeTruthy();
      },
      { timeout: 8000 },
    );

    // Uncheck "Save browser session" after the test completed — the checkbox
    // has no side effect on testStatus/testCredentialId, so this must not be
    // able to skip cleanup of the now-orphaned temp credential.
    fireEvent.click(
      screen.getByLabelText("Save browser session for future logins"),
    );

    fireEvent.click(screen.getByRole("button", { name: "Update" }));

    await waitFor(() => {
      expect(deleteMock).toHaveBeenCalledWith("/credentials/temp-cred-id");
    });
    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith(
        "/credentials/real-cred-id/update",
        expect.objectContaining({
          credential: expect.objectContaining({
            password: "rotated-password",
          }),
        }),
      );
    });

    // The bug this guards against: the old code renamed the throwaway temp
    // credential in place of updating the real one. renameCredentialMutation
    // is the only path that PATCHes a credential by the *temp* id — assert
    // it never fired.
    for (const call of patchMock.mock.calls) {
      expect(call[0]).not.toBe("/credentials/temp-cred-id");
    }
  }, 15_000);
});
