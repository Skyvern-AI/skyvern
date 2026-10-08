import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { GoogleOAuthCredential } from "@/api/types";
import { Integrations } from "./Integrations";

const mocks = vi.hoisted(() => ({
  useGoogleOAuthCredentials: vi.fn(),
  startAuthorize: vi.fn(),
  assign: vi.fn(),
}));

vi.mock("@/hooks/useGoogleOAuthCredentials", async (importActual) => {
  const actual =
    await importActual<typeof import("@/hooks/useGoogleOAuthCredentials")>();
  return {
    ...actual,
    useGoogleOAuthCredentials: mocks.useGoogleOAuthCredentials,
  };
});

// The client-config form issues its own network calls; it is irrelevant here.
vi.mock("@/components/GoogleOAuthClientConfigForm", () => ({
  GoogleOAuthClientConfigForm: () => null,
}));

function credential(
  id: string,
  name: string,
  state: string,
): GoogleOAuthCredential {
  return {
    id,
    organization_id: "o_1",
    credential_name: name,
    state,
    scopes_granted: ["https://www.googleapis.com/auth/spreadsheets"],
    scopes_requested: ["https://www.googleapis.com/auth/spreadsheets"],
    created_at: "2026-07-20T00:00:00Z",
    modified_at: "2026-07-20T00:00:00Z",
  };
}

function installHook(credentials: GoogleOAuthCredential[]) {
  mocks.startAuthorize.mockResolvedValue({
    authorize_url: "https://accounts.google.com/o/oauth2/v2/auth?x=1",
    state: "st",
  });
  mocks.useGoogleOAuthCredentials.mockReturnValue({
    credentials,
    isFetching: false,
    startAuthorize: mocks.startAuthorize,
    isStartingAuthorize: false,
    deleteCredential: vi.fn(),
    isDeletingCredential: false,
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  Object.defineProperty(window, "location", {
    value: { origin: "https://app.example.com", assign: mocks.assign },
    writable: true,
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Integrations reconnect", () => {
  it("shows the needs-reconnect state for an expired connection", () => {
    installHook([credential("goac_expired", "Sheets Bot", "error")]);
    render(<Integrations />);

    expect(screen.getByText("Needs reconnect")).toBeTruthy();
    expect(screen.getByRole("button", { name: /reconnect/i })).toBeTruthy();
  });

  it("re-authenticates in place, preserving the credential id", async () => {
    installHook([credential("goac_expired", "Sheets Bot", "error")]);
    render(<Integrations />);

    fireEvent.click(screen.getByRole("button", { name: /reconnect/i }));

    await waitFor(() => expect(mocks.startAuthorize).toHaveBeenCalledTimes(1));
    // The existing id is forwarded so the reconnected connection keeps its identity.
    expect(mocks.startAuthorize).toHaveBeenCalledWith(
      expect.objectContaining({ credential_id: "goac_expired" }),
    );
    await waitFor(() =>
      expect(mocks.assign).toHaveBeenCalledWith(
        "https://accounts.google.com/o/oauth2/v2/auth?x=1",
      ),
    );
  });

  it("offers reconnect on an active connection too", () => {
    installHook([credential("goac_active", "Sheets Bot", "active")]);
    render(<Integrations />);

    expect(screen.getByText("Active")).toBeTruthy();
    expect(screen.getByRole("button", { name: /reconnect/i })).toBeTruthy();
  });
});

describe("Integrations Gmail", () => {
  it("connects for reading only by default and adds sending to a connection in place", async () => {
    installHook([
      {
        ...credential("goac_mail", "Mail Bot", "active"),
        email_address: "mail@example.com",
        scopes_granted: ["https://www.googleapis.com/auth/gmail.readonly"],
      },
    ]);
    render(<Integrations />);
    const choices = screen.getByRole("group", { name: /What can Skyvern do/ });

    fireEvent.click(choices.nextElementSibling!);
    fireEvent.click(
      screen.getByRole("button", { name: "Enable send email for Mail Bot" }),
    );

    await waitFor(() => expect(mocks.startAuthorize).toHaveBeenCalledTimes(2));
    expect(mocks.startAuthorize.mock.calls.map(([args]) => args)).toMatchObject(
      [
        { scope_profile: "gmail" },
        { credential_id: "goac_mail", scope_profile: "gmail_send" },
      ],
    );
  });
});
