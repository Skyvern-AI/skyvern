// @vitest-environment jsdom

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { getClient } from "@/api/AxiosClient";
import type { CredentialApiResponse } from "@/api/types";
import { copyText } from "@/util/copyText";
import { CredentialItem } from "./CredentialItem";

vi.mock("@/api/AxiosClient", () => ({ getClient: vi.fn() }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));
vi.mock("@/store/useCredentialTestStore", () => ({
  useCredentialTestStore: (
    selector: (state: { activeTest: null }) => unknown,
  ) => selector({ activeTest: null }),
}));
vi.mock("@/util/copyText", () => ({ copyText: vi.fn() }));
vi.mock("@/components/ui/use-toast", () => ({
  toast: vi.fn(),
}));
vi.mock("./CredentialFolderSelector", () => ({
  CredentialFolderSelector: () => <button>Folder</button>,
}));
vi.mock("./DeleteCredentialButton", () => ({
  DeleteCredentialButton: () => <button>Delete</button>,
}));
vi.mock("./CredentialsModal", () => ({
  CredentialsModal: () => null,
}));

const mockedGetClient = vi.mocked(getClient);
const mockedCopyText = vi.mocked(copyText);

function makePasswordCredential(
  overrides: Partial<CredentialApiResponse> = {},
): CredentialApiResponse {
  return {
    credential_id: "cred_1",
    credential_type: "password",
    name: "Example Login",
    credential: {
      username: "user@example.com",
      totp_type: "authenticator",
      totp_identifier: null,
    },
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("CredentialItem TOTP code preview", () => {
  it("loads and copies the current authenticator code on demand", async () => {
    mockedGetClient.mockResolvedValue({
      get: vi.fn().mockResolvedValue({
        data: { code: "123456", seconds_remaining: 24 },
      }),
    } as never);
    mockedCopyText.mockResolvedValue(true);

    render(<CredentialItem credential={makePasswordCredential()} />);

    expect(screen.getAllByText("••••••••").length).toBeGreaterThan(1);

    fireEvent.click(screen.getByRole("button", { name: "Show 2FA code" }));

    await waitFor(() => {
      expect(screen.getByText("123 456")).toBeTruthy();
      expect(screen.getByText("24s")).toBeTruthy();
    });
    expect(mockedGetClient).toHaveBeenCalledWith(null, "sans-api-v1");

    fireEvent.click(screen.getByRole("button", { name: "Copy 2FA code" }));

    await waitFor(() => {
      expect(mockedCopyText).toHaveBeenCalledWith("123456");
    });
  });

  it("shows an API error when the saved authenticator code cannot be loaded", async () => {
    mockedGetClient.mockResolvedValue({
      get: vi.fn().mockRejectedValue({
        isAxiosError: true,
        response: {
          data: {
            detail: {
              error_code: "invalid_authenticator_key",
              message: "Saved authenticator key is invalid.",
            },
          },
        },
      }),
    } as never);

    render(<CredentialItem credential={makePasswordCredential()} />);

    fireEvent.click(screen.getByRole("button", { name: "Show 2FA code" }));

    await waitFor(() => {
      expect(
        screen.getByText("Saved authenticator key is invalid."),
      ).toBeTruthy();
    });
  });

  it("refreshes a hidden authenticator code without revealing it", async () => {
    const getMock = vi
      .fn()
      .mockResolvedValueOnce({
        data: { code: "123456", seconds_remaining: 24 },
      })
      .mockResolvedValueOnce({
        data: { code: "654321", seconds_remaining: 20 },
      });
    mockedGetClient.mockResolvedValue({ get: getMock } as never);

    render(<CredentialItem credential={makePasswordCredential()} />);

    fireEvent.click(screen.getByRole("button", { name: "Show 2FA code" }));
    await waitFor(() => {
      expect(screen.getByText("123 456")).toBeTruthy();
    });

    fireEvent.click(screen.getByRole("button", { name: "Hide 2FA code" }));
    expect(screen.queryByText("123 456")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Refresh 2FA code" }));
    await waitFor(() => {
      expect(getMock).toHaveBeenCalledTimes(2);
    });
    expect(screen.queryByText("654 321")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Show 2FA code" }));
    expect(screen.getByText("654 321")).toBeTruthy();
    expect(getMock).toHaveBeenCalledTimes(2);
  });

  it("clears the displayed code after it expires", async () => {
    mockedGetClient.mockResolvedValue({
      get: vi.fn().mockResolvedValue({
        data: { code: "123456", seconds_remaining: 1 },
      }),
    } as never);

    render(<CredentialItem credential={makePasswordCredential()} />);

    fireEvent.click(screen.getByRole("button", { name: "Show 2FA code" }));

    await waitFor(() => {
      expect(screen.getByText("123 456")).toBeTruthy();
      expect(
        screen.getByRole("button", { name: "Copy 2FA code" }),
      ).toBeTruthy();
    });

    await waitFor(
      () => {
        expect(screen.queryByText("123 456")).toBeNull();
        const copyButton = screen.getByRole("button", {
          name: "Copy 2FA code",
        }) as HTMLButtonElement;
        expect(copyButton.disabled).toBe(true);
        expect(
          screen.getByText("2FA code expired. Refresh to load a new code."),
        ).toBeTruthy();
      },
      { timeout: 3000 },
    );
  });
});
