import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { GoogleReconnectCard } from "./GoogleReconnectCard";
import type { GoogleConnectionNotice } from "../narrativeState";
import { PageSlotsProvider, type PermissionsState } from "@/store/PageSlots";

const { hookState, refetchCredentials, startAuthorize, storeIntegration } =
  vi.hoisted(() => ({
    hookState: {
      current: {
        credentials: [] as Array<{
          id: string;
          state: string;
          scopes_granted?: string[];
          credential_name?: string;
          email_address?: string | null;
        }>,
        isLoading: false,
        isFetching: false,
        error: null as Error | null,
      },
    },
    refetchCredentials: vi.fn(),
    startAuthorize: vi.fn(),
    storeIntegration: vi.fn(),
  }));

vi.mock("@/hooks/useGoogleOAuthCredentials", () => ({
  useGoogleOAuthCredentials: () => ({
    ...hookState.current,
    refetchCredentials,
    startAuthorize,
  }),
  isGoogleOAuthCredentialActive: (credential: { state: string }) =>
    credential.state === "active",
  hasGoogleOAuthCredentialScopes: (
    credential: { scopes_granted?: string[] },
    requiredScopes: readonly string[],
  ) =>
    requiredScopes.every((scope) => credential.scopes_granted?.includes(scope)),
}));

vi.mock("@/routes/integrations/googleOAuth", async (importOriginal) => ({
  ...(await importOriginal()),
  storeGoogleOAuthIntegrationIdForState: storeIntegration,
}));

const notice = {
  provider: "google" as const,
  connectionId: "goac_error",
  displayName: "Work Sheets",
  condition: "unusable" as const,
  scopeProfile: "google_sheets" as const,
  origin: "workflow" as const,
};

const SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets";
const GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly";

function setupNotice(
  overrides: Partial<GoogleConnectionNotice> = {},
): GoogleConnectionNotice {
  return {
    provider: "google",
    connectionId: null,
    displayName: null,
    condition: "unbound",
    scopeProfile: "gmail",
    origin: "setup",
    choices: [],
    ...overrides,
  };
}

function openablePopup() {
  return {
    location: { assign: vi.fn() },
    close: vi.fn(),
    closed: false,
    opener: window as Window | null,
    sessionStorage: { setItem: vi.fn() },
  };
}

const memberPermissions: PermissionsState = {
  status: "ready",
  data: {
    organization_id: "org_1",
    user_id: "user_1",
    auth_kind: "bearer",
    org_role: "org:member",
    role: "member",
    capabilities: [],
  },
};

describe("GoogleReconnectCard", () => {
  beforeEach(() => {
    hookState.current = {
      credentials: [],
      isLoading: false,
      isFetching: false,
      error: null,
    };
    refetchCredentials.mockReset().mockResolvedValue({});
    startAuthorize.mockReset();
    storeIntegration.mockReset();
    vi.unstubAllEnvs();
  });

  it("disables OAuth reconnect for Members", () => {
    render(
      <PageSlotsProvider value={{ permissions: memberPermissions }}>
        <GoogleReconnectCard notice={notice} />
      </PageSlotsProvider>,
    );

    const reconnectButton = screen.getByRole("button", {
      name: "Admin required",
    });
    expect(reconnectButton.hasAttribute("disabled")).toBe(true);
    fireEvent.click(reconnectButton);
    expect(startAuthorize).not.toHaveBeenCalled();
  });

  it("shows success only when a fresh lookup says the exact connection is active", () => {
    hookState.current.credentials = [
      {
        id: "goac_error",
        state: "active",
        scopes_granted: ["https://www.googleapis.com/auth/spreadsheets"],
      },
    ];
    const { rerender } = render(<GoogleReconnectCard notice={notice} />);
    expect(screen.queryByRole("button", { name: "Reconnect" })).toBeNull();
    expect(screen.getByRole("status").textContent).toContain(
      "Work Sheets reconnected",
    );

    hookState.current.isFetching = true;
    rerender(<GoogleReconnectCard notice={notice} />);
    expect(screen.getByRole("button", { name: "Reconnect" })).not.toBeNull();

    hookState.current.isFetching = false;
    hookState.current.credentials = [
      {
        id: "another",
        state: "active",
        scopes_granted: ["https://www.googleapis.com/auth/spreadsheets"],
      },
    ];
    rerender(<GoogleReconnectCard notice={notice} />);
    expect(screen.getByRole("button", { name: "Reconnect" })).not.toBeNull();

    hookState.current = {
      credentials: [
        {
          id: "goac_error",
          state: "active",
          scopes_granted: ["https://www.googleapis.com/auth/spreadsheets"],
        },
      ],
      isLoading: false,
      isFetching: false,
      error: new Error("offline"),
    };
    rerender(<GoogleReconnectCard notice={notice} />);
    expect(screen.getByRole("button", { name: "Reconnect" })).not.toBeNull();
  });

  it("keeps reconnect available when an active connection lacks Sheets access", () => {
    hookState.current.credentials = [
      {
        id: "goac_error",
        state: "active",
        scopes_granted: ["https://www.googleapis.com/auth/gmail.readonly"],
      },
    ];

    render(<GoogleReconnectCard notice={notice} />);

    expect(screen.getByRole("button", { name: "Reconnect" })).not.toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("does not offer an impossible reconnect for a missing connection", () => {
    render(
      <GoogleReconnectCard
        notice={{
          provider: "google",
          connectionId: "goac_missing",
          displayName: null,
          condition: "missing",
          scopeProfile: "google_sheets",
          origin: "workflow",
        }}
      />,
    );

    expect(screen.queryByRole("button", { name: "Reconnect" })).toBeNull();
    expect(
      screen.getByText("Google Sheets connection is no longer available"),
    ).not.toBeNull();
    expect(startAuthorize).not.toHaveBeenCalled();
  });

  it("reserves the tab synchronously and authorizes the same id with a stable callback", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "https://api.skyvern.com/api/v1");
    let resolveAuthorize: (value: {
      authorize_url: string;
      state: string;
    }) => void = () => {};
    startAuthorize.mockReturnValue(
      new Promise((resolve) => {
        resolveAuthorize = resolve;
      }),
    );
    const assign = vi.fn();
    const setItem = vi.fn();
    const popup = {
      location: { assign },
      close: vi.fn(),
      opener: window,
      sessionStorage: { setItem },
    };
    const open = vi
      .spyOn(window, "open")
      .mockReturnValue(popup as unknown as Window);
    render(<GoogleReconnectCard notice={notice} />);

    fireEvent.click(screen.getByRole("button", { name: "Reconnect" }));

    expect(open).toHaveBeenCalledWith(
      "",
      "skyvern-google-oauth-google_sheets:goac_error",
      expect.stringContaining("popup"),
    );
    expect(popup.opener).toBeNull();
    expect(setItem).toHaveBeenCalledWith("skyvern:google-oauth-popup", "1");
    expect(startAuthorize).toHaveBeenCalledWith({
      redirect_uri: "https://app.skyvern.com/integrations/google/callback",
      app_origin: window.location.origin,
      credential_id: "goac_error",
      scope_profile: "google_sheets",
    });
    expect(assign).not.toHaveBeenCalled();

    resolveAuthorize({
      authorize_url: "https://accounts.google.com/oauth",
      state: "state_1",
    });
    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith("https://accounts.google.com/oauth"),
    );
    expect(storeIntegration).toHaveBeenCalledWith("state_1", "google_sheets");
    expect(storeIntegration).toHaveBeenCalledWith(
      "state_1",
      "google_sheets",
      popup,
    );
    open.mockRestore();
  });

  it("shows a blocked popup on the card and retries from the same card", async () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    render(<GoogleReconnectCard notice={notice} />);
    fireEvent.click(screen.getByRole("button", { name: "Reconnect" }));
    expect(startAuthorize).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toContain("Allow pop-ups");
    expect(screen.queryByRole("status")).toBeNull();

    const popup = openablePopup();
    open.mockReturnValue(popup as unknown as Window);
    startAuthorize.mockResolvedValue({
      authorize_url: "https://accounts.google.com/oauth",
      state: "retry",
    });
    fireEvent.click(screen.getByRole("button", { name: "Reconnect again" }));
    await waitFor(() => expect(popup.location.assign).toHaveBeenCalled());
    expect(screen.queryByRole("alert")).toBeNull();
    open.mockRestore();
  });

  it("connects without an existing id and becomes a picker after credential refresh", async () => {
    const onSelect = vi.fn();
    const popup = {
      location: { assign: vi.fn() },
      close: vi.fn(),
      opener: window,
      sessionStorage: { setItem: vi.fn() },
    };
    const open = vi
      .spyOn(window, "open")
      .mockReturnValue(popup as unknown as Window);
    startAuthorize.mockResolvedValue({
      authorize_url: "https://accounts.google.com/oauth",
      state: "new-account",
    });
    const unbound = {
      provider: "google" as const,
      connectionId: null,
      displayName: null,
      condition: "unbound" as const,
      scopeProfile: "google_sheets" as const,
      origin: "workflow" as const,
      choices: [],
    };
    const { rerender } = render(
      <GoogleReconnectCard notice={unbound} onSelect={onSelect} disabled />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    await waitFor(() => expect(popup.location.assign).toHaveBeenCalled());
    expect(startAuthorize.mock.calls[0]?.[0]).not.toHaveProperty(
      "credential_id",
    );
    expect(popup.opener).toBeNull();

    hookState.current.credentials = [
      {
        id: "goac_new",
        credential_name: "New Sheets",
        state: "active",
        scopes_granted: ["https://www.googleapis.com/auth/spreadsheets"],
      },
      {
        id: "goac_mail",
        credential_name: "Mail only",
        state: "active",
        scopes_granted: [],
      },
    ];
    rerender(<GoogleReconnectCard notice={unbound} onSelect={onSelect} />);
    expect(screen.queryByRole("button", { name: "Connect" })).toBeNull();
    expect(screen.queryByText("Mail only")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /New Sheets/ }));
    expect(onSelect).toHaveBeenCalledWith("goac_new");

    onSelect.mockClear();
    rerender(
      <GoogleReconnectCard notice={unbound} onSelect={onSelect} disabled />,
    );
    fireEvent.click(screen.getByRole("button", { name: /New Sheets/ }));
    expect(onSelect).not.toHaveBeenCalled();
    open.mockRestore();
  });

  it("lets Members choose a saved account for an unbound Sheets block", () => {
    hookState.current.credentials = [
      {
        id: "goac_saved",
        credential_name: "Saved Sheets",
        state: "active",
        scopes_granted: ["https://www.googleapis.com/auth/spreadsheets"],
      },
    ];
    const onSelect = vi.fn();
    const unbound = {
      provider: "google" as const,
      connectionId: null,
      displayName: null,
      condition: "unbound" as const,
      scopeProfile: "google_sheets" as const,
      origin: "workflow" as const,
      choices: [],
    };

    render(
      <PageSlotsProvider value={{ permissions: memberPermissions }}>
        <GoogleReconnectCard notice={unbound} onSelect={onSelect} />
      </PageSlotsProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: /Saved Sheets/ }));
    expect(onSelect).toHaveBeenCalledWith("goac_saved");
  });

  it("allows saved choices during a list-fetch error but keeps historical choices disabled", () => {
    hookState.current.error = new Error("offline");
    const onSelect = vi.fn();
    const unbound = {
      provider: "google" as const,
      connectionId: null,
      displayName: null,
      condition: "unbound" as const,
      scopeProfile: "google_sheets" as const,
      origin: "workflow" as const,
      choices: [
        {
          connection_id: "goac_saved",
          name: "Saved Sheets",
          state: "active",
          email_address: null,
        },
      ],
    };
    const { rerender } = render(
      <GoogleReconnectCard notice={unbound} onSelect={onSelect} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Saved Sheets/ }));
    expect(onSelect).toHaveBeenCalledWith("goac_saved");
    onSelect.mockClear();
    rerender(
      <GoogleReconnectCard notice={unbound} onSelect={onSelect} disabled />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Saved Sheets/ }));
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("asks Google for exactly the read-only Gmail profile on a Gmail setup card", async () => {
    const popup = openablePopup();
    const open = vi
      .spyOn(window, "open")
      .mockReturnValue(popup as unknown as Window);
    startAuthorize.mockResolvedValue({
      authorize_url: "https://accounts.google.com/oauth",
      state: "gmail-state",
    });
    hookState.current.credentials = [
      {
        id: "goac_sheets",
        credential_name: "Sheets only",
        state: "active",
        scopes_granted: [SHEETS_SCOPE],
      },
    ];

    render(<GoogleReconnectCard notice={setupNotice()} onSelect={vi.fn()} />);
    expect(screen.queryByText("Sheets only")).toBeNull();
    expect(screen.getByText("Connect Gmail (read only)")).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    await waitFor(() => expect(popup.location.assign).toHaveBeenCalled());
    const body = startAuthorize.mock.calls[0]?.[0];
    expect(body.scope_profile).toBe("gmail");
    expect(body).not.toHaveProperty("credential_id");
    expect(JSON.stringify(body)).not.toContain("send");
    expect(storeIntegration).toHaveBeenCalledWith("gmail-state", "gmail");
    open.mockRestore();
  });

  it("gives Gmail and Sheets setup cards separate consent popups", async () => {
    const gmailPopup = openablePopup();
    const sheetsPopup = openablePopup();
    const open = vi
      .spyOn(window, "open")
      .mockReturnValueOnce(gmailPopup as unknown as Window)
      .mockReturnValueOnce(sheetsPopup as unknown as Window);
    startAuthorize.mockResolvedValue({
      authorize_url: "https://accounts.google.com/oauth",
      state: "state_1",
    });

    render(
      <>
        <GoogleReconnectCard notice={setupNotice()} onSelect={vi.fn()} />
        <GoogleReconnectCard
          notice={setupNotice({ scopeProfile: "google_sheets" })}
          onSelect={vi.fn()}
        />
      </>,
    );
    for (const button of screen.getAllByRole("button", { name: "Connect" })) {
      fireEvent.click(button);
    }

    await waitFor(() => expect(sheetsPopup.location.assign).toHaveBeenCalled());
    const names = open.mock.calls.map((call) => call[1]);
    expect(names).toEqual([
      "skyvern-google-oauth-gmail:unbound",
      "skyvern-google-oauth-google_sheets:unbound",
    ]);
    expect(gmailPopup.location.assign).toHaveBeenCalled();
    open.mockRestore();
  });

  it("shows a role denial from the server on the card without a connected claim", async () => {
    const popup = openablePopup();
    const open = vi
      .spyOn(window, "open")
      .mockReturnValue(popup as unknown as Window);
    startAuthorize.mockRejectedValue({ response: { status: 403 } });

    render(<GoogleReconnectCard notice={setupNotice()} />);
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    expect((await screen.findByRole("alert")).textContent).toContain(
      "admin access",
    );
    expect(popup.close).toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "Connect again" }),
    ).not.toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
    open.mockRestore();
  });

  it("reports a popup closed without a new connection as not completed and binds nothing", async () => {
    const onSelect = vi.fn();
    const popup = openablePopup();
    const open = vi
      .spyOn(window, "open")
      .mockReturnValue(popup as unknown as Window);
    startAuthorize.mockResolvedValue({
      authorize_url: "https://accounts.google.com/oauth",
      state: "cancelled",
    });

    render(<GoogleReconnectCard notice={setupNotice()} onSelect={onSelect} />);
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    await waitFor(() => expect(popup.location.assign).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: "Waiting for Google…" }));
    expect(screen.queryByRole("alert")).toBeNull();

    popup.closed = true;
    expect(
      (await screen.findByRole("alert", {}, { timeout: 2000 })).textContent,
    ).toContain("not completed");
    expect(refetchCredentials).toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "Connect again" }),
    ).not.toBeNull();
    expect(onSelect).not.toHaveBeenCalled();
    open.mockRestore();
  });

  it("makes a successful setup reconnect a one-row choice that waits for a click", () => {
    const onSelect = vi.fn();
    hookState.current.credentials = [
      {
        id: "goac_mail",
        credential_name: "Inbox",
        state: "active",
        scopes_granted: [GMAIL_READ_SCOPE],
      },
    ];

    render(
      <GoogleReconnectCard
        notice={setupNotice({
          connectionId: "goac_mail",
          displayName: "Inbox",
          condition: "unusable",
          choices: undefined,
        })}
        onSelect={onSelect}
      />,
    );

    expect(screen.queryByRole("status")).toBeNull();
    expect(onSelect).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /Inbox/ }));
    expect(onSelect).toHaveBeenCalledWith("goac_mail");
  });
});
