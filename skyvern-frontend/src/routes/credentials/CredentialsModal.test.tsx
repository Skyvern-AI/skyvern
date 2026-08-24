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
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CredentialApiResponse } from "@/api/types";

import {
  CredentialAuthenticatorSupportProvider,
  type CredentialAdditionalTwoFactorMethod,
} from "./CredentialAuthenticatorSupportContext";
import { getAuthenticatorKeyError } from "./credentialTotpValidation";
import { CredentialsModal } from "./CredentialsModal";
import { CredentialModalTypes } from "./useCredentialModalState";

const postMock = vi.hoisted(() => vi.fn());
const patchMock = vi.hoisted(() => vi.fn());
const deleteMock = vi.hoisted(() => vi.fn());
const getMock = vi.hoisted(() => vi.fn());
const toastMock = vi.hoisted(() => vi.fn());
const mockEmailCredentials = vi.hoisted(() => ({
  google: vi.fn(),
  microsoft: vi.fn(),
}));

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

vi.mock("@/hooks/useGoogleOAuthCredentials", async (importActual) => {
  const actual =
    await importActual<typeof import("@/hooks/useGoogleOAuthCredentials")>();
  return {
    ...actual,
    useGoogleOAuthCredentials: mockEmailCredentials.google,
  };
});

vi.mock("@/hooks/useMicrosoftOAuthCredentials", async (importActual) => {
  const actual =
    await importActual<typeof import("@/hooks/useMicrosoftOAuthCredentials")>();
  return {
    ...actual,
    useMicrosoftOAuthCredentials: mockEmailCredentials.microsoft,
  };
});

vi.mock("@/routes/workflows/hooks/useCredentialsQuery", () => ({
  useCredentialsQuery: () => ({ data: [] }),
}));

// Default off so the existing legacy-path tests are unaffected; the browser-memory
// describe flips it on per-test.
const useFeatureFlagMock = vi.hoisted(() => vi.fn(() => false));
vi.mock("@/hooks/useFeatureFlag", () => ({
  useFeatureFlag: useFeatureFlagMock,
}));

function installEmailCredentialHooks() {
  mockEmailCredentials.google.mockReturnValue({
    credentials: [
      {
        id: "google-mail",
        organization_id: "org_1",
        credential_name: "Default",
        state: "active",
        scopes_granted: ["https://www.googleapis.com/auth/gmail.readonly"],
        email_address: "connected@gmail.test",
        created_at: "2026-07-30T00:00:00Z",
        modified_at: "2026-07-30T00:00:00Z",
      },
    ],
    isLoading: false,
    isFetching: false,
  });
  mockEmailCredentials.microsoft.mockReturnValue({
    credentials: [],
    isLoading: false,
    isFetching: false,
  });
}

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

function renderPasswordCredentialsModal(
  additionalTwoFactorMethods?: CredentialAdditionalTwoFactorMethod[],
  onOpenChange = vi.fn(),
  onCredentialCreated?: (id: string, name?: string) => void,
) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <CredentialAuthenticatorSupportProvider
        value={{ additionalTwoFactorMethods }}
      >
        <MemoryRouter>
          <CredentialsModal
            isOpen
            onOpenChange={onOpenChange}
            onCredentialCreated={onCredentialCreated}
            overrideType={CredentialModalTypes.PASSWORD}
          />
        </MemoryRouter>
      </CredentialAuthenticatorSupportProvider>
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
  auto_profile_disabled: false,
  tested_url: "https://example.com/login",
  user_context: null,
  save_browser_session_intent: true,
  folder_id: null,
  proxy_location: null,
  proxy_session_id: null,
};

function renderEditPasswordCredentialsModal(
  credential = editingPasswordCredential,
  additionalTwoFactorMethods?: CredentialAdditionalTwoFactorMethod[],
) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <CredentialAuthenticatorSupportProvider
        value={{ additionalTwoFactorMethods }}
      >
        <MemoryRouter>
          <CredentialsModal
            isOpen
            onOpenChange={vi.fn()}
            overrideType={CredentialModalTypes.PASSWORD}
            editingCredential={credential}
            onStartBackgroundTest={vi.fn()}
          />
        </MemoryRouter>
      </CredentialAuthenticatorSupportProvider>
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

  it("does not validate the key for email, text, extension, or disabled 2FA methods", () => {
    expect(
      getAuthenticatorKeyError({ totp: "", totp_type: "email" }),
    ).toBeNull();
    expect(
      getAuthenticatorKeyError({ totp: "", totp_type: "text" }),
    ).toBeNull();
    expect(
      getAuthenticatorKeyError({ totp: "", totp_type: "none" }),
    ).toBeNull();
    expect(
      getAuthenticatorKeyError({ totp: "", totp_type: "security_device" }),
    ).toBeNull();
  });
});

describe("CredentialsModal additional two-factor methods", () => {
  beforeEach(() => {
    installEmailCredentialHooks();
  });

  it("validates extension state and runs its post-save callback with the created id", async () => {
    const onSaved = vi.fn().mockResolvedValue(undefined);
    const additionalMethod: CredentialAdditionalTwoFactorMethod = {
      value: "security_device",
      requestType: "none",
      label: "Security Device",
      initialState: { deviceCode: "" },
      renderFields: ({ state, setState }) => (
        <label>
          Device code
          <input
            aria-label="Device code"
            value={String(state.deviceCode ?? "")}
            onChange={(event) =>
              setState({ ...state, deviceCode: event.target.value })
            }
          />
        </label>
      ),
      validate: (state) =>
        String(state.deviceCode ?? "").trim()
          ? null
          : "Device code is required.",
      onSaved,
    };
    postMock.mockResolvedValueOnce({
      data: { credential_id: "cred-security-device", name: "credentials" },
    });
    renderPasswordCredentialsModal([additionalMethod]);

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
    fireEvent.click(screen.getByText("Security Device"));

    const saveButton = screen.getByRole("button", { name: "Save" });
    expect((saveButton as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("Device code is required.")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Device code"), {
      target: { value: "device-value" },
    });
    expect((saveButton as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith(
        "/credentials",
        expect.objectContaining({
          credential: {
            username: "user@example.com",
            password: "password",
            totp: null,
            totp_type: "none",
            totp_identifier: null,
          },
        }),
      );
    });
    await waitFor(() => {
      expect(onSaved).toHaveBeenCalledWith(
        expect.objectContaining({
          credentialId: "cred-security-device",
          state: { deviceCode: "device-value" },
          wasSelected: true,
          previouslyConfigured: false,
        }),
      );
    });
    const request = postMock.mock.calls[0]?.[1] as {
      credential: Record<string, unknown>;
    };
    expect(request.credential).not.toHaveProperty("security_device");
  });

  it("surfaces a post-save extension failure as a partial save", async () => {
    const onOpenChange = vi.fn();
    const additionalMethod: CredentialAdditionalTwoFactorMethod = {
      value: "security_device",
      requestType: "none",
      label: "Security Device",
      renderFields: () => null,
      validate: () => null,
      onSaved: vi
        .fn()
        .mockRejectedValue(axiosErrorWithDetail("Device setup failed.")),
    };
    postMock.mockResolvedValueOnce({
      data: { credential_id: "cred-security-device", name: "credentials" },
    });
    renderPasswordCredentialsModal([additionalMethod], onOpenChange);

    await waitFor(() => {
      expect(screen.getByDisplayValue("credentials")).toBeTruthy();
    });
    const usernameInput = Array.from(
      document.querySelectorAll<HTMLInputElement>("input"),
    ).find(
      (input) =>
        input.type === "text" && input.value === "" && input.placeholder === "",
    );
    fireEvent.change(usernameInput as HTMLInputElement, {
      target: { value: "user@example.com" },
    });
    fireEvent.change(document.querySelector('input[type="password"]')!, {
      target: { value: "password" },
    });
    fireEvent.click(screen.getByText("Two-Factor Authentication"));
    fireEvent.click(screen.getByText("Security Device"));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(toastMock).toHaveBeenCalledWith({
        title: "Partial save",
        description: "Device setup failed.",
        variant: "destructive",
      });
    });
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("rolls back the created credential when the passkey material attach fails", async () => {
    const onSaved = vi
      .fn()
      .mockRejectedValue(axiosErrorWithDetail("Passkey setup failed."));
    const onCredentialCreated = vi.fn();
    const additionalMethod: CredentialAdditionalTwoFactorMethod = {
      value: "passkey",
      requestType: "passkey",
      label: "Passkey",
      renderFields: () => null,
      validate: () => null,
      onSaved,
    };
    postMock.mockResolvedValueOnce({
      data: { credential_id: "cred-passkey", name: "credentials" },
    });
    deleteMock.mockResolvedValueOnce({ data: {} });
    renderPasswordCredentialsModal(
      [additionalMethod],
      vi.fn(),
      onCredentialCreated,
    );

    await waitFor(() => {
      expect(screen.getByDisplayValue("credentials")).toBeTruthy();
    });
    const usernameInput = Array.from(
      document.querySelectorAll<HTMLInputElement>("input"),
    ).find(
      (input) =>
        input.type === "text" && input.value === "" && input.placeholder === "",
    );
    fireEvent.change(usernameInput as HTMLInputElement, {
      target: { value: "user@example.com" },
    });
    fireEvent.change(document.querySelector('input[type="password"]')!, {
      target: { value: "password" },
    });
    fireEvent.click(screen.getByText("Two-Factor Authentication"));
    fireEvent.click(screen.getByText("Passkey"));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(onSaved).toHaveBeenCalledWith(
        expect.objectContaining({ credentialId: "cred-passkey" }),
      );
    });
    await waitFor(() => {
      expect(deleteMock).toHaveBeenCalledWith("/credentials/cred-passkey");
    });
    expect(onCredentialCreated).not.toHaveBeenCalled();
  });

  it("runs the configured method callback after turning two-factor off", async () => {
    const onSaved = vi.fn().mockResolvedValue(undefined);
    const additionalMethod: CredentialAdditionalTwoFactorMethod = {
      value: "security_device",
      requestType: "none",
      label: "Security Device",
      initialState: { deviceCode: "" },
      renderFields: () => null,
      validate: () => null,
      onSaved,
    };
    postMock.mockResolvedValueOnce({
      data: { credential_id: "real-cred-id", name: "Acme Login" },
    });
    renderEditPasswordCredentialsModal(
      {
        ...editingPasswordCredential,
        credential: {
          username: "user@example.com",
          totp_type: "security_device",
          totp_identifier: null,
        },
      } as unknown as CredentialApiResponse,
      [additionalMethod],
    );

    fireEvent.click(screen.getAllByLabelText("Edit credential values")[0]!);
    fireEvent.change(screen.getByPlaceholderText("••••••••"), {
      target: { value: "password" },
    });
    // Collapsing the Two-Factor Authentication section turns 2FA off (no None tile).
    fireEvent.click(screen.getByText("Two-Factor Authentication"));
    fireEvent.click(screen.getByRole("button", { name: "Update" }));

    // Removing a saved 2FA method now requires confirming an in-app dialog.
    fireEvent.click(await screen.findByRole("button", { name: "Remove" }));

    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith(
        "/credentials/real-cred-id/update",
        expect.objectContaining({
          credential: expect.objectContaining({ totp_type: "none" }),
        }),
      );
    });
    await waitFor(() => {
      expect(onSaved).toHaveBeenCalledWith(
        expect.objectContaining({
          credentialId: "real-cred-id",
          wasSelected: false,
          previouslyConfigured: true,
        }),
      );
    });
  });

  it.each(["Authenticator App", "Email", "Text Message"])(
    "keeps the inline test affordance for %s",
    (methodLabel) => {
      renderPasswordCredentialsModal();

      fireEvent.click(screen.getByText("Two-Factor Authentication"));
      fireEvent.click(screen.getByText(methodLabel));

      const inlineTestToggle = screen.getByLabelText(
        "Save browser session for future logins",
      ) as HTMLButtonElement;
      expect(inlineTestToggle.disabled).toBe(false);
      fireEvent.click(inlineTestToggle);
      expect(screen.getByRole("button", { name: "Test" })).toBeTruthy();
      expect(
        screen.queryByText(
          "Inline login testing is not available for this two-factor method.",
        ),
      ).toBeNull();
    },
  );
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
    const submittedCredential = postMock.mock.calls[0]?.[1] as {
      credential: Record<string, unknown>;
    };
    expect(submittedCredential.credential).not.toHaveProperty(
      "additionalTwoFactorState",
    );
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

  it("shows only the partial-save toast, not a success toast, when the metadata PATCH fails", async () => {
    postMock.mockResolvedValueOnce({
      data: { credential_id: "real-cred-id", name: "Acme Login" },
    });
    patchMock.mockRejectedValueOnce(new Error("patch failed"));

    renderEditPasswordCredentialsModal();

    fireEvent.click(screen.getAllByLabelText("Edit credential values")[0]!);
    fireEvent.change(
      document.querySelector('input[type="password"]') as HTMLInputElement,
      { target: { value: "rotated-password" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Update" }));

    await waitFor(() => {
      expect(patchMock).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(toastMock).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Partial save",
          // Flag-off the PATCH omits browser profile / IP, so the copy names
          // login instructions and sequential settings only — byte-identical
          // to the flag-off partial-save message.
          description:
            "Credential updated, but login instructions and sequential settings could not be saved. Please try editing again.",
        }),
      );
    });
    expect(toastMock).not.toHaveBeenCalledWith(
      expect.objectContaining({ title: "Credential updated" }),
    );
  }, 15_000);
});

describe("CredentialsModal edit-mode password preservation", () => {
  const noPasswordLabel = "This login has no password";

  it("omits password from the overwrite payload when the field is left untouched", async () => {
    postMock.mockResolvedValueOnce({
      data: { credential_id: "real-cred-id", name: "Acme Login" },
    });
    patchMock.mockResolvedValue({ data: {} });
    renderEditPasswordCredentialsModal();

    fireEvent.click(screen.getAllByLabelText("Edit credential values")[0]!);
    const usernameInput = screen.getByDisplayValue("user@example.com");
    fireEvent.change(usernameInput, {
      target: { value: "renamed@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Update" }));

    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith(
        "/credentials/real-cred-id/update",
        expect.objectContaining({
          credential: expect.objectContaining({
            username: "renamed@example.com",
          }),
        }),
      );
    });
    const submitted = postMock.mock.calls[0]?.[1] as {
      credential: Record<string, unknown>;
    };
    expect(submitted.credential).not.toHaveProperty("password");
  }, 10_000);

  it("sends an empty password only when the no-password box is checked", async () => {
    postMock.mockResolvedValueOnce({
      data: { credential_id: "real-cred-id", name: "Acme Login" },
    });
    patchMock.mockResolvedValue({ data: {} });
    renderEditPasswordCredentialsModal();

    fireEvent.click(screen.getAllByLabelText("Edit credential values")[0]!);
    fireEvent.click(screen.getByRole("checkbox", { name: noPasswordLabel }));
    fireEvent.click(screen.getByRole("button", { name: "Update" }));

    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith(
        "/credentials/real-cred-id/update",
        expect.objectContaining({
          credential: expect.objectContaining({ password: "" }),
        }),
      );
    });
  }, 10_000);

  it("still sends a re-entered password on the overwrite", async () => {
    postMock.mockResolvedValueOnce({
      data: { credential_id: "real-cred-id", name: "Acme Login" },
    });
    patchMock.mockResolvedValue({ data: {} });
    renderEditPasswordCredentialsModal();

    fireEvent.click(screen.getAllByLabelText("Edit credential values")[0]!);
    fireEvent.change(screen.getByPlaceholderText("••••••••"), {
      target: { value: "rotated-password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Update" }));

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
  }, 10_000);

  it("hides the no-password box until the values group is opened", () => {
    renderEditPasswordCredentialsModal();

    expect(
      screen.queryByRole("checkbox", { name: noPasswordLabel }),
    ).toBeNull();
    fireEvent.click(screen.getAllByLabelText("Edit credential values")[0]!);
    expect(
      screen.getByRole("checkbox", { name: noPasswordLabel }),
    ).toBeTruthy();
  });

  it("does not offer the no-password box on create, where a blank field already means none", async () => {
    renderPasswordCredentialsModal();

    await waitFor(() => {
      expect(screen.getByDisplayValue("credentials")).toBeTruthy();
    });
    expect(
      screen.queryByRole("checkbox", { name: noPasswordLabel }),
    ).toBeNull();
  });
});

describe("CredentialsModal password-less inline test", () => {
  it("enables Test on a create-mode credential with no password", async () => {
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
    fireEvent.change(usernameInput as HTMLInputElement, {
      target: { value: "user@example.com" },
    });
    fireEvent.click(
      screen.getByLabelText("Save browser session for future logins"),
    );
    fireEvent.change(screen.getByPlaceholderText("https://example.com/login"), {
      target: { value: "https://example.com/login" },
    });

    const testButton = screen.getByRole("button", {
      name: "Test",
    }) as HTMLButtonElement;
    expect(testButton.disabled).toBe(false);

    postMock.mockResolvedValueOnce({
      data: { credential_id: "temp-cred-id", workflow_run_id: "wr-1" },
    });
    fireEvent.click(testButton);
    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith(
        "/credentials/test-login",
        expect.objectContaining({
          username: "user@example.com",
          password: "",
        }),
      );
    });
  }, 10_000);

  it("keeps Test disabled in edit mode until the password is entered or declared absent", async () => {
    renderEditPasswordCredentialsModal();

    fireEvent.click(screen.getAllByLabelText("Edit credential values")[0]!);
    const testButton = screen.getByRole("button", {
      name: "Test",
    }) as HTMLButtonElement;
    // Blank here means "keep the stored password", which the inline test cannot read.
    expect(testButton.disabled).toBe(true);

    fireEvent.click(
      screen.getByRole("checkbox", { name: "This login has no password" }),
    );
    expect(
      (screen.getByRole("button", { name: "Test" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
  });
});

describe("CredentialsModal browser-memory profile section (flag on)", () => {
  beforeEach(() => {
    useFeatureFlagMock.mockImplementation(
      (flag?: string) => flag === "browser_memory_v1",
    );
  });
  afterEach(() => {
    useFeatureFlagMock.mockImplementation(() => false);
  });

  it("create mode shows the auto-save caption and a collapsed Advanced section", async () => {
    renderPasswordCredentialsModal();
    await waitFor(() => {
      expect(screen.getByDisplayValue("credentials")).toBeTruthy();
    });
    expect(
      screen.getByText(/Skyvern saves this login.s browser state/i),
    ).toBeTruthy();
    const advanced = screen.getByRole("button", { name: "Advanced" });
    // Both browser-memory controls live behind the disclosure.
    expect(
      screen.queryByRole("checkbox", {
        name: "Don't automatically save or reuse a browser profile for this credential",
      }),
    ).toBeNull();
    expect(
      screen.queryByRole("checkbox", {
        name: "Keep the same IP for this credential",
      }),
    ).toBeNull();
    fireEvent.click(advanced);
    expect(
      screen.getByRole("checkbox", {
        name: "Keep the same IP for this credential",
      }),
    ).toBeTruthy();
    expect(
      screen
        .getByRole("checkbox", {
          name: "Don't automatically save or reuse a browser profile for this credential",
        })
        .getAttribute("data-state"),
    ).toBe("unchecked");
    expect(screen.queryByText("Browser profile")).toBeNull();
  });

  it("create mode submits the IP pin and automatic-profile opt-out", async () => {
    postMock.mockResolvedValueOnce({
      data: { credential_id: "c1", name: "credentials" },
    });
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
    fireEvent.change(usernameInput as HTMLInputElement, {
      target: { value: "user@example.com" },
    });
    fireEvent.change(
      document.querySelector('input[type="password"]') as HTMLInputElement,
      { target: { value: "password" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Advanced" }));
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "Keep the same IP for this credential",
      }),
    );
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "Don't automatically save or reuse a browser profile for this credential",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith(
        "/credentials",
        expect.objectContaining({
          browser_profile_id: null,
          pin_saved_session_ip: true,
          auto_profile_disabled: true,
        }),
      );
    });
  }, 10_000);

  it("edit mode shows the Browser profile select and Advanced section", async () => {
    getMock.mockImplementation((url: string) =>
      url.includes("/browser_profiles/")
        ? Promise.resolve({
            data: {
              browser_profile_id: "existing-profile-id",
              name: "Saved login",
              is_managed: false,
              linked_credential_name: "Acme Login",
            },
          })
        : Promise.resolve({ data: [] }),
    );
    renderEditPasswordCredentialsModal();
    await waitFor(() => {
      expect(screen.getByText("Browser profile")).toBeTruthy();
    });
    expect(screen.getByRole("combobox")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Advanced" })).toBeTruthy();
    expect(
      screen.queryByText(/Skyvern saves this login.s browser state/i),
    ).toBeNull();
  });

  it("edit mode persists opting back in without unlinking the profile", async () => {
    patchMock.mockResolvedValueOnce({
      data: {
        ...editingPasswordCredential,
        auto_profile_disabled: false,
      },
    });
    const view = renderEditPasswordCredentialsModal({
      ...editingPasswordCredential,
      auto_profile_disabled: true,
    });
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Advanced" })).toBeTruthy();
    });
    fireEvent.click(screen.getByRole("button", { name: "Advanced" }));
    const optOut = screen.getByRole("checkbox", {
      name: "Don't automatically save or reuse a browser profile for this credential",
    });
    expect(optOut.getAttribute("data-state")).toBe("checked");
    fireEvent.click(optOut);
    fireEvent.click(screen.getByRole("button", { name: "Update" }));

    await waitFor(() => {
      expect(patchMock).toHaveBeenCalledWith(
        "/credentials/real-cred-id",
        expect.objectContaining({
          auto_profile_disabled: false,
          browser_profile_id: "existing-profile-id",
        }),
      );
    });

    view.unmount();
    renderEditPasswordCredentialsModal({
      ...editingPasswordCredential,
      auto_profile_disabled: false,
    });
    fireEvent.click(screen.getByRole("button", { name: "Advanced" }));
    expect(
      screen
        .getByRole("checkbox", {
          name: "Don't automatically save or reuse a browser profile for this credential",
        })
        .getAttribute("data-state"),
    ).toBe("unchecked");
  });

  it("keeps the proxy IP-pin UI for card credentials under the flag", async () => {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <CredentialsModal
            isOpen
            onOpenChange={vi.fn()}
            overrideType={CredentialModalTypes.CREDIT_CARD}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    // The password-only "Keep the same IP" checkbox does not cover card/secret,
    // so the proxy-pin UI must stay for them even when the flag is on.
    await waitFor(() => {
      expect(screen.getByText("Use a consistent IP address")).toBeTruthy();
    });
    expect(
      screen.queryByText(/Skyvern saves this login.s browser state/i),
    ).toBeNull();
  });

  const pinnedEditCredential: CredentialApiResponse = {
    ...editingPasswordCredential,
    proxy_session_id: "psi_9f8a7b6c5d4e3f2a1b",
    pin_saved_session_ip: true,
  };

  const pinnedNoSessionCredential: CredentialApiResponse = {
    ...editingPasswordCredential,
    proxy_session_id: null,
    pin_saved_session_ip: true,
  };

  function renderEditModalFor(cred: CredentialApiResponse) {
    getMock.mockImplementation((url: string) =>
      url.includes("/browser_profiles/")
        ? Promise.resolve({
            data: {
              browser_profile_id: "existing-profile-id",
              name: "Saved login",
              is_managed: false,
              linked_credential_name: "Acme Login",
            },
          })
        : Promise.resolve({ data: [] }),
    );
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
            editingCredential={cred}
            onStartBackgroundTest={vi.fn()}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  async function openAdvanced() {
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Advanced" })).toBeTruthy();
    });
    fireEvent.click(screen.getByRole("button", { name: "Advanced" }));
  }

  it("shows the IP session identity and Rotate when the credential is pinned", async () => {
    renderEditModalFor(pinnedEditCredential);
    await openAdvanced();
    await waitFor(() => {
      expect(screen.getByText(/psi_9f8a7b/)).toBeTruthy();
    });
    expect(screen.getByRole("button", { name: "Rotate" })).toBeTruthy();
  });

  it("hides the IP session identity and Rotate when no session is pinned", async () => {
    renderEditModalFor(pinnedNoSessionCredential);
    await openAdvanced();
    await waitFor(() => {
      expect(
        screen.getByText("Keep the same IP for this credential"),
      ).toBeTruthy();
    });
    expect(screen.queryByRole("button", { name: "Rotate" })).toBeNull();
    expect(screen.queryByText(/IP session:/)).toBeNull();
  });

  it("Rotate sends rotate_proxy_session_id on save", async () => {
    patchMock.mockResolvedValueOnce({
      data: { credential_id: "real-cred-id", name: "Acme Login" },
    });
    renderEditModalFor(pinnedEditCredential);
    await openAdvanced();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Rotate" })).toBeTruthy();
    });
    fireEvent.click(screen.getByRole("button", { name: "Rotate" }));
    fireEvent.click(screen.getByRole("button", { name: "Update" }));
    await waitFor(() => {
      expect(patchMock).toHaveBeenCalledWith(
        "/credentials/real-cred-id",
        expect.objectContaining({ rotate_proxy_session_id: true }),
      );
    });
  }, 10_000);
});

describe("CredentialsModal copilot-context tested_url default", () => {
  function renderCopilotPasswordModal(opts: {
    defaultTestUrl?: string;
    onCredentialCreated?: (id: string, name?: string) => void;
  }) {
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
            defaultTestUrl={opts.defaultTestUrl}
            onCredentialCreated={opts.onCredentialCreated}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  async function fillUsername() {
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
  }

  async function fillUsernameAndPassword() {
    await fillUsername();
    const passwordInput = document.querySelector('input[type="password"]');
    expect(passwordInput).toBeTruthy();
    fireEvent.change(passwordInput as HTMLInputElement, {
      target: { value: "password" },
    });
  }

  it("persists tested_url from defaultTestUrl on a plain create (no test run)", async () => {
    postMock.mockResolvedValueOnce({
      data: { credential_id: "cred-x", name: "credentials" },
    });
    patchMock.mockResolvedValue({ data: {} });
    const onCredentialCreated = vi.fn();
    renderCopilotPasswordModal({
      defaultTestUrl: "https://news.ycombinator.com/login",
      onCredentialCreated,
    });
    await fillUsernameAndPassword();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/credentials", expect.anything()),
    );
    await waitFor(() =>
      expect(patchMock).toHaveBeenCalledWith(
        "/credentials/cred-x",
        expect.objectContaining({
          tested_url: "https://news.ycombinator.com/login",
        }),
      ),
    );
    expect(onCredentialCreated).toHaveBeenCalledWith("cred-x", "credentials");
  }, 10_000);

  it("saves a password-less credential when the password is left empty", async () => {
    postMock.mockResolvedValueOnce({
      data: { credential_id: "cred-z", name: "credentials" },
    });
    const onCredentialCreated = vi.fn();
    renderCopilotPasswordModal({ onCredentialCreated });
    await fillUsername();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith(
        "/credentials",
        expect.objectContaining({
          credential: expect.objectContaining({
            username: "user@example.com",
            password: "",
          }),
        }),
      ),
    );
    expect(onCredentialCreated).toHaveBeenCalledWith("cred-z", "credentials");
  }, 10_000);

  it("sends no tested_url when defaultTestUrl is absent (modal from elsewhere)", async () => {
    postMock.mockResolvedValueOnce({
      data: { credential_id: "cred-y", name: "credentials" },
    });
    const onCredentialCreated = vi.fn();
    renderCopilotPasswordModal({ onCredentialCreated });
    await fillUsernameAndPassword();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(onCredentialCreated).toHaveBeenCalledWith("cred-y", "credentials"),
    );
    expect(patchMock).not.toHaveBeenCalled();
  }, 10_000);
});

describe("CredentialsModal run-sequentially toggle", () => {
  const sequentialLabel = /Run workflows sequentially for this credential/i;

  function renderEditModalFor(cred: CredentialApiResponse) {
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
            editingCredential={cred}
            onStartBackgroundTest={vi.fn()}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it("reflects run_sequentially=false as an unchecked toggle", async () => {
    renderEditModalFor(editingPasswordCredential);
    const toggle = await screen.findByLabelText(sequentialLabel);
    expect(toggle.getAttribute("aria-checked")).toBe("false");
  });

  it("helper copy says scheduled/sync runs fail closed and does not over-promise", async () => {
    renderEditModalFor(editingPasswordCredential);
    await screen.findByLabelText(sequentialLabel);
    expect(
      screen.getByText(
        /scheduled or sync-triggered runs are not yet supported and fail closed/i,
      ),
    ).toBeTruthy();
    expect(screen.queryByText(/run without serialization/i)).toBeNull();
  });

  it("reflects run_sequentially=true as a checked toggle (readback)", async () => {
    renderEditModalFor({
      ...editingPasswordCredential,
      run_sequentially: true,
    });
    const toggle = await screen.findByLabelText(sequentialLabel);
    expect(toggle.getAttribute("aria-checked")).toBe("true");
  });

  it("persists run_sequentially via PATCH and shows success on a toggle-only edit", async () => {
    patchMock.mockResolvedValueOnce({
      data: { credential_id: "real-cred-id", name: "Acme Login" },
    });
    renderEditModalFor(editingPasswordCredential);
    const toggle = await screen.findByLabelText(sequentialLabel);
    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole("button", { name: "Update" }));

    await waitFor(() => {
      expect(patchMock).toHaveBeenCalledWith(
        "/credentials/real-cred-id",
        expect.objectContaining({ run_sequentially: true }),
      );
    });
    await waitFor(() => {
      expect(toastMock).toHaveBeenCalledWith(
        expect.objectContaining({ title: "Credential saved" }),
      );
    });
  }, 10_000);

  it("surfaces the save failure and shows no success toast when the PATCH rejects", async () => {
    patchMock.mockRejectedValueOnce(axiosErrorWithDetail("nope"));
    renderEditModalFor(editingPasswordCredential);
    const toggle = await screen.findByLabelText(sequentialLabel);
    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole("button", { name: "Update" }));

    await waitFor(() => {
      expect(patchMock).toHaveBeenCalledWith(
        "/credentials/real-cred-id",
        expect.objectContaining({ run_sequentially: true }),
      );
    });
    expect(toastMock).not.toHaveBeenCalledWith(
      expect.objectContaining({ title: "Credential saved" }),
    );
  }, 10_000);
});
