// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { getClient } from "@/api/AxiosClient";
import { OtpType } from "@/api/types";
import { useTotpCodesQuery } from "@/hooks/useTotpCodesQuery";
import { CredentialsTotpTab } from "./CredentialsTotpTab";

vi.mock("@/api/AxiosClient", () => ({ getClient: vi.fn() }));

vi.mock("@/hooks/useTotpCodesQuery", () => ({
  useTotpCodesQuery: vi.fn(),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

const mockedGetClient = vi.mocked(getClient);
const mockedUseTotpCodesQuery = vi.mocked(useTotpCodesQuery);

function renderTab(variant?: "twoFactor" | "magicLink") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <CredentialsTotpTab variant={variant} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("CredentialsTotpTab", () => {
  it("pins magic-link listings and submissions", async () => {
    const post = vi.fn().mockResolvedValue({});
    mockedGetClient.mockResolvedValue({ post } as never);
    mockedUseTotpCodesQuery.mockReturnValue({
      data: [],
      isLoading: false,
      isFetching: false,
      isFeatureUnavailable: false,
    } as unknown as ReturnType<typeof useTotpCodesQuery>);

    renderTab("magicLink");

    expect(mockedUseTotpCodesQuery).toHaveBeenCalledWith({
      params: {
        totp_identifier: undefined,
        otp_type: OtpType.MagicLink,
        limit: 50,
      },
    });
    expect(document.querySelector("#totp-type-filter")).toBeNull();
    expect(document.querySelector("#totp-type-input")).toBeNull();
    expect(
      screen.getByRole("heading", {
        name: "Automatic magic links from your inbox",
      }),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "Skyvern can find magic links in a connected Gmail inbox without manual forwarding.",
      ),
    ).toBeTruthy();
    expect(
      screen.getByRole("heading", { name: "Push a Magic Link" }),
    ).toBeTruthy();
    expect(
      screen.getByText(/Prefer to send magic links programmatically/),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "No magic links yet. Paste a magic link message above or connect an email account.",
      ),
    ).toBeTruthy();

    fireEvent.change(
      screen.getByPlaceholderText("Email receiving the magic link"),
      {
        target: { value: " user@example.com " },
      },
    );
    fireEvent.change(screen.getByLabelText("Verification content"), {
      target: { value: " https://example.com/login?token=abc " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send Magic Link" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/credentials/totp", {
        totp_identifier: "user@example.com",
        content: "https://example.com/login?token=abc",
        type: OtpType.MagicLink,
        source: "manual_ui",
      });
    });
  });

  it("keeps the two-factor default on all types with its selector visible", () => {
    mockedUseTotpCodesQuery.mockReturnValue({
      data: [],
      isLoading: false,
      isFetching: false,
      isFeatureUnavailable: false,
    } as unknown as ReturnType<typeof useTotpCodesQuery>);

    renderTab();

    expect(mockedUseTotpCodesQuery).toHaveBeenCalledWith({
      params: {
        totp_identifier: undefined,
        otp_type: undefined,
        limit: 50,
      },
    });
    expect(document.querySelector("#totp-type-filter")).toBeTruthy();
    expect(document.querySelector("#totp-type-filter")?.textContent).toBe(
      "All types",
    );
    expect(document.querySelector("#totp-type-input")?.textContent).toBe(
      "Numeric code",
    );
    expect(
      screen.getByRole("heading", {
        name: "Automatic 2FA from your inbox",
      }),
    ).toBeTruthy();
    expect(
      screen.getByRole("heading", { name: "Push a 2FA Code" }),
    ).toBeTruthy();
    expect(
      screen.getByText(/Prefer to send codes programmatically/),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "No 2FA codes yet. Paste a verification message above or configure automatic forwarding.",
      ),
    ).toBeTruthy();
  });

  it("updates the push-card copy when Magic link is selected", () => {
    mockedUseTotpCodesQuery.mockReturnValue({
      data: [],
      isLoading: false,
      isFetching: false,
      isFeatureUnavailable: false,
    } as unknown as ReturnType<typeof useTotpCodesQuery>);

    renderTab();
    const otpTypeSelect = document.querySelector("#totp-type-input");
    expect(otpTypeSelect).not.toBeNull();
    fireEvent.keyDown(otpTypeSelect!, { key: "ArrowDown" });
    fireEvent.click(screen.getByRole("option", { name: "Magic link" }));

    expect(
      screen.getByRole("heading", { name: "Push a Magic Link" }),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "Paste the magic link message you received. Skyvern extracts the link and attaches it to the relevant run.",
      ),
    ).toBeTruthy();
    expect(
      screen.getByText(/Prefer to send magic links programmatically/),
    ).toBeTruthy();
    expect(
      screen.queryByRole("heading", { name: "Push a 2FA Code" }),
    ).toBeNull();
  });

  it("uses the magic-link unavailable alert copy", () => {
    mockedUseTotpCodesQuery.mockReturnValue({
      data: undefined,
      isLoading: false,
      isFetching: false,
      isFeatureUnavailable: true,
    } as unknown as ReturnType<typeof useTotpCodesQuery>);

    renderTab("magicLink");

    expect(screen.getByText("Magic link listing unavailable")).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toContain(
      "Once available, this tab will automatically populate with magic links.",
    );
  });
});
