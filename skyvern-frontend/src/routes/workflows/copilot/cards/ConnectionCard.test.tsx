import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { GoogleOAuthCredential } from "@/api/types";

import { ConnectionCard } from "./ConnectionCard";
import { googleConnectionChanged } from "./googleConnectionChanged";

const { googleCredentials, microsoftCredentials, startAuthorize } = vi.hoisted(
  () => ({
    googleCredentials: { current: [] as Partial<GoogleOAuthCredential>[] },
    microsoftCredentials: { current: [] as Record<string, unknown>[] },
    startAuthorize: vi.fn(),
  }),
);

vi.mock("@/hooks/useGoogleOAuthCredentials", async (importOriginal) => ({
  ...(await importOriginal()),
  useGoogleOAuthCredentials: () => ({
    credentials: googleCredentials.current,
    isLoading: false,
    refetchCredentials: () =>
      Promise.resolve({ data: googleCredentials.current }),
    startAuthorize,
  }),
}));

vi.mock("@/hooks/useMicrosoftOAuthCredentials", async (importOriginal) => ({
  ...(await importOriginal()),
  useMicrosoftOAuthCredentials: () => ({
    credentials: microsoftCredentials.current,
    isLoading: false,
  }),
}));

vi.mock("@/routes/integrations/googleOAuth", async (importOriginal) => ({
  ...(await importOriginal()),
  storeGoogleOAuthIntegrationIdForState: vi.fn(),
}));

const SHEETS = "https://www.googleapis.com/auth/spreadsheets";
const GMAIL = "https://www.googleapis.com/auth/gmail.readonly";

const row = (
  id: string,
  scopes: string[],
  state = "active",
): GoogleOAuthCredential => ({
  id,
  organization_id: "o_1",
  credential_name: id,
  state,
  scopes_granted: scopes,
  created_at: "2026-01-01T00:00:00Z",
  modified_at: "2026-01-01T00:00:00Z",
});

describe("googleConnectionChanged", () => {
  const gmailOnly = row("goac_gmail", [GMAIL]);

  it("counts a new account as a completed connect", () => {
    expect(
      googleConnectionChanged(
        [gmailOnly],
        [gmailOnly, row("goac_new", [SHEETS])],
        null,
      ),
    ).toBe(true);
  });

  it("counts new scopes on the reconnected account, which keeps its id", () => {
    expect(
      googleConnectionChanged(
        [gmailOnly],
        [row("goac_gmail", [GMAIL, SHEETS])],
        "goac_gmail",
      ),
    ).toBe(true);
  });

  it("counts an errored account that comes back active with the same scopes", () => {
    expect(
      googleConnectionChanged(
        [row("goac_gmail", [GMAIL, SHEETS], "error")],
        [row("goac_gmail", [GMAIL, SHEETS])],
        "goac_gmail",
      ),
    ).toBe(true);
  });

  it("reports a popup closed with nothing changed as not completed", () => {
    expect(
      googleConnectionChanged(
        [gmailOnly],
        [row("goac_gmail", [GMAIL])],
        "goac_gmail",
      ),
    ).toBe(false);
    expect(googleConnectionChanged([gmailOnly], [gmailOnly], null)).toBe(false);
  });
});

describe("ConnectionCard", () => {
  it("confirms before setting an account on a connection with none recorded", async () => {
    googleCredentials.current = [
      {
        id: "goac_error",
        state: "error",
        credential_name: "Work Sheets",
        scopes_granted: [],
        account_recorded: false,
      },
    ];
    const popup = {
      location: { assign: vi.fn() },
      close: vi.fn(),
      closed: false,
      opener: window as Window | null,
      sessionStorage: { setItem: vi.fn() },
    };
    const open = vi
      .spyOn(window, "open")
      .mockReturnValue(popup as unknown as Window);
    startAuthorize.mockResolvedValue({
      authorize_url: "https://accounts.google.com/oauth",
      state: "set-account",
    });
    render(
      <ConnectionCard
        card={{
          integrationId: "google_sheets",
          integrationName: "Google Sheets",
          provider: "google",
          scopeProfile: "google_sheets",
          requiredScopes: [SHEETS],
          reconnectConnectionId: "goac_error",
        }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Reconnect" }));
    expect(open).not.toHaveBeenCalled();
    expect(startAuthorize).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    await waitFor(() =>
      expect(startAuthorize).toHaveBeenCalledWith(
        expect.objectContaining({
          credential_id: "goac_error",
          scope_profile: "google_sheets",
          set_account: true,
        }),
      ),
    );
    open.mockRestore();
  });

  it("settles on Connected once the reconnected account has the scopes", () => {
    googleCredentials.current = [
      {
        ...row("goac_work", [GMAIL, SHEETS]),
        credential_name: "Work",
        email_address: "work@example.com",
      },
    ];
    render(
      <ConnectionCard
        card={{
          integrationId: "google_sheets",
          integrationName: "Google Sheets",
          provider: "google",
          scopeProfile: "google_sheets",
          requiredScopes: [SHEETS],
          reconnectConnectionId: "goac_work",
        }}
      />,
    );

    expect(screen.getByText("Connected")).toBeTruthy();
    expect(
      screen.getByText("Work (work@example.com) can use Google Sheets."),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^(Re)?connect/ })).toBeNull();
  });

  it("names the admin block and offers Try again when the popup closes with no change", async () => {
    googleCredentials.current = [];
    const popup = {
      location: { assign: vi.fn() },
      close: vi.fn(),
      closed: false,
      opener: window as Window | null,
      sessionStorage: { setItem: vi.fn() },
    };
    const open = vi
      .spyOn(window, "open")
      .mockReturnValue(popup as unknown as Window);
    startAuthorize.mockResolvedValue({
      authorize_url: "https://accounts.google.com/oauth",
      state: "connect",
    });
    render(
      <ConnectionCard
        card={{
          integrationId: "google_sheets",
          integrationName: "Google Sheets",
          provider: "google",
          scopeProfile: "google_sheets",
          requiredScopes: [SHEETS],
          reconnectConnectionId: null,
        }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    await screen.findByRole("button", { name: "Waiting for sign-in…" });
    popup.closed = true;

    const alert = await screen.findByRole("alert", {}, { timeout: 2000 });
    expect(alert.textContent).toContain("Google Workspace administrator");
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Connect again" })).toBeNull();
    open.mockRestore();
  });

  it("names Outlook, not Google, in an Outlook card's account choices", () => {
    microsoftCredentials.current = [
      {
        id: "moac_mail",
        organization_id: "o_1",
        credential_name: "Mail",
        state: "active",
        scopes_granted: ["Mail.Read"],
      },
    ];
    render(
      <ConnectionCard
        card={{
          integrationId: "outlook",
          integrationName: "Outlook",
          provider: "microsoft",
          scopeProfile: null,
          requiredScopes: ["Mail.Read"],
          reconnectConnectionId: null,
        }}
      />,
    );

    expect(screen.getByText("Choose an Outlook account")).toBeTruthy();
    expect(screen.queryByText(/Google/)).toBeNull();
    microsoftCredentials.current = [];
  });
});
