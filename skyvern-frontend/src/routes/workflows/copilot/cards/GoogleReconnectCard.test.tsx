import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { GoogleReconnectCard } from "./GoogleReconnectCard";

const { hookState, startAuthorize, storeIntegration, toast } = vi.hoisted(
  () => ({
    hookState: {
      current: {
        credentials: [] as Array<{
          id: string;
          state: string;
          scopes_granted?: string[];
          credential_name?: string;
        }>,
        isLoading: false,
        isFetching: false,
        error: null as Error | null,
      },
    },
    startAuthorize: vi.fn(),
    storeIntegration: vi.fn(),
    toast: vi.fn(),
  }),
);

vi.mock("@/hooks/useGoogleOAuthCredentials", () => ({
  useGoogleOAuthCredentials: () => ({
    ...hookState.current,
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

vi.mock("@/components/ui/use-toast", () => ({ toast }));
vi.mock("@/routes/integrations/googleOAuth", async (importOriginal) => ({
  ...(await importOriginal()),
  storeGoogleOAuthIntegrationIdForState: storeIntegration,
}));

const notice = {
  provider: "google" as const,
  connectionId: "goac_error",
  displayName: "Work Sheets",
  condition: "unusable" as const,
};

describe("GoogleReconnectCard", () => {
  beforeEach(() => {
    hookState.current = {
      credentials: [],
      isLoading: false,
      isFetching: false,
      error: null,
    };
    startAuthorize.mockReset();
    storeIntegration.mockReset();
    toast.mockReset();
    vi.unstubAllEnvs();
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
      "skyvern-google-oauth",
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

  it("does not leave the studio when the browser blocks the new tab", () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    render(<GoogleReconnectCard notice={notice} />);
    fireEvent.click(screen.getByRole("button", { name: "Reconnect" }));
    expect(startAuthorize).not.toHaveBeenCalled();
    expect(toast).toHaveBeenCalled();
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
  it("allows saved choices during a list-fetch error but keeps historical choices disabled", () => {
    hookState.current.error = new Error("offline");
    const onSelect = vi.fn();
    const unbound = {
      provider: "google" as const,
      connectionId: null,
      displayName: null,
      condition: "unbound" as const,
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
});
