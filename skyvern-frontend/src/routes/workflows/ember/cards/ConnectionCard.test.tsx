import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { GoogleOAuthCredential } from "@/api/types";
import { type ConnectedGmailFlow, PageSlotsProvider } from "@/store/PageSlots";

import type { ConnectionCard as ConnectionCardData } from "../narrativeState";

import { ConnectionCard } from "./ConnectionCard";
import { googleConnectionOutcome } from "./googleConnectionOutcome";

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

vi.mock("@/hooks/useCurrentOrgId", () => ({ useCurrentOrgId: () => "o_1" }));

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

// Server stamps carry no zone; the card parses them as UTC.
const POSTED_AT = "2026-01-02T00:00:00";

const row = (
  id: string,
  scopes: string[],
  state = "active",
  createdAt = "2026-01-01T00:00:00",
): GoogleOAuthCredential => ({
  id,
  organization_id: "o_1",
  credential_name: id,
  state,
  scopes_granted: scopes,
  created_at: createdAt,
  modified_at: createdAt,
});

const GMAIL_CARD: ConnectionCardData = {
  integrationId: "gmail",
  integrationName: "Gmail",
  provider: "google",
  scopeProfile: "gmail",
  requiredScopes: [GMAIL],
  reconnectConnectionId: null,
};

const SHEETS_CARD: ConnectionCardData = {
  integrationId: "google_sheets",
  integrationName: "Google Sheets",
  provider: "google",
  scopeProfile: "google_sheets",
  requiredScopes: [SHEETS],
  reconnectConnectionId: null,
};

function stubPopup(credentialId = "goac_new") {
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
    state: "attempt",
    credential_id: credentialId,
  });
  return { popup, open };
}

afterEach(() => {
  googleCredentials.current = [];
  microsoftCredentials.current = [];
  startAuthorize.mockReset();
  sessionStorage.clear();
  vi.restoreAllMocks();
});

describe("googleConnectionOutcome", () => {
  const gmailOnly = row("goac_gmail", [GMAIL]);
  const fresh = { credentialId: "goac_new", reconnect: false };
  const reconnect = { credentialId: "goac_gmail", reconnect: true };

  it.each([
    [
      "a new account with the scopes",
      [gmailOnly],
      [gmailOnly, row("goac_new", [SHEETS])],
      fresh,
      { kind: "connected", connectionId: "goac_new" },
    ],
    [
      "new scopes on the reconnected account, which keeps its id",
      [gmailOnly],
      [row("goac_gmail", [GMAIL, SHEETS])],
      reconnect,
      { kind: "connected", connectionId: "goac_gmail" },
    ],
    [
      "a new account missing a required scope",
      [gmailOnly],
      [gmailOnly, row("goac_new", [GMAIL])],
      fresh,
      { kind: "partial", connectionId: "goac_new" },
    ],
    [
      "a reconnected account that changed but still misses a scope",
      [row("goac_gmail", [GMAIL], "error")],
      [gmailOnly],
      reconnect,
      { kind: "partial", connectionId: "goac_gmail" },
    ],
    [
      "a reconnect closed with nothing changed",
      [gmailOnly],
      [gmailOnly],
      reconnect,
      { kind: "not_completed" },
    ],
    // The list was still loading or had failed at the click, so an existing account is missing from the snapshot.
    [
      "an existing account absent from an empty snapshot",
      [],
      [row("goac_old", [SHEETS])],
      fresh,
      { kind: "not_completed" },
    ],
    [
      "another account added while the popup was open",
      [gmailOnly],
      [gmailOnly, row("goac_other", [SHEETS])],
      fresh,
      { kind: "not_completed" },
    ],
    [
      "a reconnected account that was not loaded at the click",
      [],
      [row("goac_gmail", [GMAIL, SHEETS])],
      reconnect,
      { kind: "not_completed" },
    ],
    [
      "an attempt the server did not identify",
      [],
      [row("goac_new", [SHEETS])],
      null,
      { kind: "unattributed" },
    ],
  ])("settles %s", (_, before, after, attempt, expected) => {
    expect(googleConnectionOutcome(before, after, attempt, [SHEETS])).toEqual(
      expected,
    );
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
      screen.getByText(
        (_, node) =>
          node?.textContent ===
          "Work (work@example.com) can use Google Sheets.",
      ),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^(Re)?connect/ })).toBeNull();
  });

  it.each([
    [
      "a popup closed with no change, with no guessed cause",
      () => stubPopup().popup,
      "The connection was not completed.",
      "Try again",
    ],
    [
      "a blocked popup",
      () => {
        vi.spyOn(window, "open").mockReturnValue(null);
        return null;
      },
      "Your browser blocked the sign-in window. Allow pop-ups for this site, then try again.",
      "Connect again",
    ],
  ])("keeps %s across a reload", async (_, openPopup, message, retryLabel) => {
    const popup = openPopup();
    const { unmount } = render(
      <ConnectionCard card={SHEETS_CARD} turnId="t1" postedAt={POSTED_AT} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    if (popup) {
      await screen.findByRole("button", { name: "Waiting for sign-in…" });
      popup.closed = true;
    }
    const alert = await screen.findByRole("alert", {}, { timeout: 2000 });
    expect(alert.textContent).toBe(message);
    expect(screen.getByRole("button", { name: retryLabel })).toBeTruthy();

    unmount();
    render(
      <ConnectionCard card={SHEETS_CARD} turnId="t1" postedAt={POSTED_AT} />,
    );
    expect(screen.getByRole("alert").textContent).toBe(message);
  });

  it.each([
    ["the account its own sign-in created", [], "goac_new"],
    // Connect stays enabled while the list loads, so the click-time snapshot can be empty.
    ["no existing account when the snapshot was empty", [], null],
    ["no account another sign-in added meanwhile", ["goac_other"], null],
  ])("sends %s as the pick", async (_, addedMeanwhile, picked) => {
    const { popup } = stubPopup("goac_new");
    const onSelect = vi.fn();
    render(
      <ConnectionCard
        card={SHEETS_CARD}
        turnId="t1"
        postedAt={POSTED_AT}
        onSelect={onSelect}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    await screen.findByRole("button", { name: "Waiting for sign-in…" });
    googleCredentials.current = [
      row("goac_old", [SHEETS]),
      ...addedMeanwhile.map((id) =>
        row(id, [SHEETS], "active", "2026-01-02T00:05:00"),
      ),
      ...(picked
        ? [row(picked, [SHEETS], "active", "2026-01-02T00:05:00")]
        : []),
    ];
    popup.closed = true;

    await waitFor(
      () =>
        expect(
          screen.queryByRole("button", { name: "Waiting for sign-in…" }),
        ).toBeNull(),
      { timeout: 2000 },
    );
    expect(onSelect.mock.calls).toEqual(picked ? [[picked]] : []);
  });

  it("keeps a partial grant visible across a reload and reconnects the partly granted account", async () => {
    const { popup } = stubPopup();
    const { unmount } = render(
      <ConnectionCard card={SHEETS_CARD} turnId="t1" postedAt={POSTED_AT} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    await screen.findByRole("button", { name: "Waiting for sign-in…" });
    googleCredentials.current = [
      {
        ...row("goac_new", [GMAIL], "active", "2026-01-02T00:05:00"),
        credential_name: "Work",
      },
    ];
    popup.closed = true;

    const alert = await screen.findByRole("alert", {}, { timeout: 2000 });
    expect(alert.textContent).toContain(
      "Work was connected without the Google Sheets permission",
    );
    unmount();
    render(
      <ConnectionCard card={SHEETS_CARD} turnId="t1" postedAt={POSTED_AT} />,
    );
    expect(screen.getByRole("alert").textContent).toContain(
      "Work was connected without the Google Sheets permission",
    );
    fireEvent.click(screen.getByRole("button", { name: "Reconnect" }));
    await waitFor(() =>
      expect(startAuthorize).toHaveBeenCalledWith(
        expect.objectContaining({
          credential_id: "goac_new",
          scope_profile: "google_sheets",
        }),
      ),
    );
  });

  it("drops a partly granted account that can no longer be reconnected and offers a fresh connect", async () => {
    stubPopup();
    startAuthorize.mockRejectedValue({ response: { status: 404 } });
    sessionStorage.setItem(
      `skyvern-ember-connection-outcome:t1:google_sheets:new`,
      JSON.stringify({ attempt: "partial", partialConnectionId: "goac_gone" }),
    );
    render(
      <ConnectionCard card={SHEETS_CARD} turnId="t1" postedAt={POSTED_AT} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Reconnect" }));

    await screen.findByRole("button", { name: "Connect" });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(
      sessionStorage.getItem(
        `skyvern-ember-connection-outcome:t1:google_sheets:new`,
      ),
    ).toBeNull();
  });

  it("offers a choice for an account from before the card, and shows one made after it as Connected but unpicked", () => {
    const onSelect = vi.fn();
    googleCredentials.current = [row("goac_old", [SHEETS])];
    const tree = () => (
      <ConnectionCard
        card={SHEETS_CARD}
        turnId="t1"
        postedAt={POSTED_AT}
        onSelect={onSelect}
      />
    );
    const { rerender } = render(tree());
    expect(screen.queryByText("Connected")).toBeNull();
    expect(screen.getByRole("button", { name: "Connect" })).toBeTruthy();
    expect(screen.getByText("Choose a Google account")).toBeTruthy();

    googleCredentials.current = [
      row("goac_old", [SHEETS]),
      {
        ...row("goac_new", [SHEETS], "active", "2026-01-02T00:05:00"),
        credential_name: "Work",
      },
    ];
    rerender(tree());
    expect(screen.getByText("Connected")).toBeTruthy();
    expect(
      screen.getByText(
        (_, node) => node?.textContent === "Work can use Google Sheets.",
      ),
    ).toBeTruthy();
    // Without a sign-in from this card, and with organization accounts, the picker asks.
    expect(onSelect).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /Work/ }));
    expect(onSelect).toHaveBeenCalledWith("goac_new");
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
  it("starts Connected Gmail for a new Gmail card, and keeps a reconnect on the native sign-in", async () => {
    startAuthorize.mockClear();
    const start = vi.fn();
    const flow: ConnectedGmailFlow = {
      status: "default",
      canConnect: true,
      busy: false,
      failure: null,
      connections: [],
      start,
    };
    const gmailCard = {
      integrationId: "gmail",
      integrationName: "Gmail",
      provider: "google" as const,
      scopeProfile: "gmail",
      requiredScopes: [GMAIL],
      reconnectConnectionId: null,
    };
    const slots = {
      connectedGmailFlow: ({
        children,
      }: {
        children: (value: ConnectedGmailFlow) => React.ReactNode;
      }) => <>{children(flow)}</>,
    };
    const open = vi.spyOn(window, "open");
    const { unmount } = render(
      <PageSlotsProvider value={slots}>
        <ConnectionCard card={gmailCard} />
      </PageSlotsProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    expect(start).toHaveBeenCalledOnce();
    expect(open).not.toHaveBeenCalled();
    expect(startAuthorize).not.toHaveBeenCalled();
    unmount();

    let driveFlow: ConnectedGmailFlow = {
      ...flow,
      connections: [{ connection_id: "ngc_drive", name: "Broker Drive" }],
    };
    // Starting this flow finishes a Connected Drive sign-in, as the real flow does once the user consents.
    function BrokerDriveFlow({
      children,
    }: {
      children: (value: ConnectedGmailFlow) => ReactNode;
    }) {
      const [current, setCurrent] = useState<ConnectedGmailFlow>(() => ({
        ...driveFlow,
        start: () =>
          setCurrent((value) => ({
            ...value,
            connections: [
              ...value.connections,
              { connection_id: "ngc_new", name: "New Drive" },
            ],
            completedConnectionId: "ngc_new",
          })),
      }));
      return <>{children(current)}</>;
    }
    const driveSlots = { connectedGoogleDriveFlow: BrokerDriveFlow };
    const picks: string[] = [];
    const driveCard = (
      <PageSlotsProvider value={driveSlots}>
        <ConnectionCard
          card={{
            integrationId: "google_drive",
            integrationName: "Google Drive",
            provider: "google",
            scopeProfile: "google_drive",
            requiredScopes: ["https://www.googleapis.com/auth/drive"],
            reconnectConnectionId: null,
            credentialBackends: ["native", "nango"],
          }}
          onSelect={(connectionId) => picks.push(connectionId)}
        />
      </PageSlotsProvider>
    );
    // An org with no native Drive account gets one Connect, and the account it brings in is the card's pick.
    const drive = render(driveCard);
    expect(
      screen.queryByRole("button", { name: "Connect Connected Google Drive" }),
    ).toBeNull();
    expect(screen.getByText("Broker Drive")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    await waitFor(() => expect(picks).toEqual(["ngc_new"]));
    drive.unmount();
    // While the flow is still deciding, the card holds one disabled Connect, so no click can start native sign-in.
    driveFlow = { ...driveFlow, status: "loading" };
    const loadingDrive = render(driveCard);
    expect(
      screen.queryByRole("button", { name: "Connect Connected Google Drive" }),
    ).toBeNull();
    expect(
      (screen.getByRole("button", { name: "Connect" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    loadingDrive.unmount();
    // An org that already has native Drive keeps both ways to connect.
    driveFlow = { ...driveFlow, status: "unavailable" };
    const nativeDrive = render(driveCard);
    expect(
      screen.getByRole("button", { name: "Connect Connected Google Drive" }),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "Connect" })).toBeTruthy();
    nativeDrive.unmount();

    const nativeOnlyDrive = render(
      <PageSlotsProvider value={driveSlots}>
        <ConnectionCard
          card={{
            integrationId: "google_drive",
            integrationName: "Google Drive",
            provider: "google",
            scopeProfile: "google_drive",
            requiredScopes: ["https://www.googleapis.com/auth/drive"],
            reconnectConnectionId: null,
            credentialBackends: ["native"],
          }}
        />
      </PageSlotsProvider>,
    );
    expect(screen.getByRole("button", { name: "Connect" })).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Connect Connected Google Drive" }),
    ).toBeNull();
    expect(screen.queryByText("Broker Drive")).toBeNull();
    // The server refuses a new native Drive sign-in unless it is for a native-only action, so this card's opens.
    stubPopup();
    startAuthorize.mockImplementation(async (request) =>
      request.native_only
        ? {
            authorize_url: "https://accounts.google.com/oauth",
            state: "attempt",
            credential_id: "goac_new",
          }
        : Promise.reject({ response: { status: 409 } }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    expect(await screen.findByText("Waiting for sign-in…")).toBeTruthy();
    expect(screen.queryByText("Could not start the connection.")).toBeNull();
    nativeOnlyDrive.unmount();
    open.mockClear();

    googleCredentials.current = [row("goac_gmail", [GMAIL], "error")];
    render(
      <PageSlotsProvider value={slots}>
        <ConnectionCard
          card={{ ...gmailCard, reconnectConnectionId: "goac_gmail" }}
        />
      </PageSlotsProvider>,
    );
    open.mockReturnValue(null);
    fireEvent.click(screen.getByRole("button", { name: "Reconnect" }));
    expect(start).toHaveBeenCalledOnce();
    expect(open).toHaveBeenCalledOnce();
    open.mockRestore();
    googleCredentials.current = [];
  });
  it.each(["default", "unavailable"] as const)(
    "offers an existing Connected Gmail account (flow %s) and picks only the account its own sign-in finished with",
    async (status) => {
      // A native account that also fits makes two choices, so nothing is picked without a sign-in.
      googleCredentials.current = [row("goac_old", [GMAIL])];
      const onSelect = vi.fn();
      let flow: ConnectedGmailFlow = {
        status,
        canConnect: true,
        busy: false,
        failure: null,
        connections: [{ connection_id: "nc_existing", name: "nc_existing" }],
        start: vi.fn(),
      };
      const Slot = ({
        children,
      }: {
        children: (value: ConnectedGmailFlow) => React.ReactNode;
      }) => <>{children(flow)}</>;
      const tree = () => (
        <PageSlotsProvider value={{ connectedGmailFlow: Slot }}>
          <ConnectionCard card={GMAIL_CARD} onSelect={onSelect} />
        </PageSlotsProvider>
      );
      const { rerender } = render(tree());
      expect(screen.queryByText("Connected")).toBeNull();
      expect(screen.getByText("nc_existing")).toBeTruthy();

      // An account that shows up in the list without this card's sign-in is not picked.
      flow = {
        ...flow,
        connections: [
          ...flow.connections,
          { connection_id: "nc_new", name: "nc_new" },
        ],
      };
      rerender(tree());
      expect(screen.getByText("Connected")).toBeTruthy();
      expect(onSelect).not.toHaveBeenCalled();

      flow = { ...flow, completedConnectionId: "nc_new" };
      rerender(tree());
      await waitFor(() => expect(onSelect.mock.calls).toEqual([["nc_new"]]));
    },
  );
  it("offers a user's one Connected Gmail account instead of Connect, and sends it only when clicked", () => {
    const onSelect = vi.fn();
    const flow: ConnectedGmailFlow = {
      status: "default",
      canConnect: false,
      busy: false,
      failure: null,
      connections: [
        {
          connection_id: "ngc_existing",
          name: "Connected Gmail",
          email_address: "o***@example.com",
        },
      ],
      start: vi.fn(),
    };
    const Slot = ({
      children,
    }: {
      children: (value: ConnectedGmailFlow) => React.ReactNode;
    }) => <>{children(flow)}</>;
    const tree = (selected: string | null) => (
      <PageSlotsProvider value={{ connectedGmailFlow: Slot }}>
        <ConnectionCard
          card={GMAIL_CARD}
          onSelect={onSelect}
          selectedConnectionId={selected}
        />
      </PageSlotsProvider>
    );
    const { rerender } = render(tree(null));

    expect(
      screen.getByText(
        (_, node) =>
          node?.textContent ===
          "Your Connected Gmail account o***@example.com is shown below. Pick it to use it here. To use a different Gmail account, reconnect or disconnect it on the Integrations page.",
      ),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Connect" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Admin required" })).toBeNull();
    expect(onSelect).not.toHaveBeenCalled();
    fireEvent.click(
      screen.getByRole("button", { name: /o\*\*\*@example.com/ }),
    );
    expect(onSelect.mock.calls).toEqual([["ngc_existing"]]);

    // Once the chat holds the pick, the one account shows as in use with no picker.
    rerender(tree("ngc_existing"));
    expect(
      screen.getByText(
        (_, node) =>
          node?.textContent ===
          "Using your Connected Gmail account o***@example.com. To use a different Gmail account, reconnect or disconnect it on the Integrations page.",
      ),
    ).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: /o\*\*\*@example.com/ }),
    ).toBeNull();
  });
  it("never falls back to native sign-in on a Connected-Gmail-only card whose flow cannot connect", () => {
    const blocked: ConnectedGmailFlow = {
      status: "unavailable",
      canConnect: false,
      busy: false,
      failure: null,
      connections: [],
      start: vi.fn(),
    };
    render(
      <PageSlotsProvider
        value={{
          connectedGmailFlow: ({
            children,
          }: {
            children: (value: ConnectedGmailFlow) => React.ReactNode;
          }) => <>{children(blocked)}</>,
        }}
      >
        <ConnectionCard
          card={{
            integrationId: "gmail",
            integrationName: "Gmail",
            provider: "google",
            scopeProfile: "gmail",
            requiredScopes: [GMAIL],
            reconnectConnectionId: null,
            connectedGmailOnly: true,
          }}
        />
      </PageSlotsProvider>,
    );

    expect(screen.queryByRole("button", { name: "Connect" })).toBeNull();
    expect(
      (
        screen.getByRole("button", {
          name: "Unavailable",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
  });
  it("switches to Connected Gmail when native sign-in is refused because Connected Gmail is the way", async () => {
    googleCredentials.current = [];
    startAuthorize.mockReset();
    startAuthorize.mockRejectedValue({ response: { status: 409 } });
    const popup = {
      location: { assign: vi.fn() },
      close: vi.fn(),
      closed: false,
      opener: null,
    };
    const open = vi
      .spyOn(window, "open")
      .mockReturnValue(popup as unknown as Window);
    const start = vi.fn();
    const onSelect = vi.fn();
    let selected: string | null = null;
    let flow: ConnectedGmailFlow = {
      status: "unavailable",
      canConnect: true,
      busy: false,
      failure: null,
      connections: [],
      start,
    };
    // One slot component across rerenders, as in the app, so the card keeps its state.
    const Slot = ({
      children,
    }: {
      children: (value: ConnectedGmailFlow) => React.ReactNode;
    }) => <>{children(flow)}</>;
    const tree = () => (
      <PageSlotsProvider
        value={{
          connectedGmailFlow: Slot,
        }}
      >
        <ConnectionCard
          onSelect={onSelect}
          selectedConnectionId={selected}
          card={{
            integrationId: "gmail",
            integrationName: "Gmail",
            provider: "google",
            scopeProfile: "gmail",
            requiredScopes: [GMAIL],
            reconnectConnectionId: null,
          }}
        />
      </PageSlotsProvider>
    );
    const { rerender } = render(tree());

    fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    await waitFor(() => expect(start).toHaveBeenCalledOnce());
    expect(popup.close).toHaveBeenCalled();
    expect(screen.queryByText("Could not start the connection.")).toBeNull();
    // After the switch, a failed Connected Gmail sign-in shows its reason and Connect retries it, not native.
    flow = { ...flow, failure: "Sign-in did not finish." };
    rerender(tree());
    expect(screen.getByText("Sign-in did not finish.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Connect again" }));
    expect(start).toHaveBeenCalledTimes(2);
    expect(startAuthorize).toHaveBeenCalledOnce();
    // A flow that can no longer connect (catalog unreadable) falls back to native sign-in, not an admin note.
    flow = { ...flow, failure: null, canConnect: false };
    rerender(tree());
    expect(screen.queryByRole("button", { name: "Admin required" })).toBeNull();
    expect(screen.getByRole("button", { name: "Connect" })).toBeTruthy();
    // A successful retry adds the account even after the flow can no longer connect.
    flow = {
      ...flow,
      connections: [{ connection_id: "ngc_new", name: "n***@example.com" }],
      completedConnectionId: "ngc_new",
    };
    rerender(tree());
    expect(screen.getByText("Connected")).toBeTruthy();
    expect(
      screen.getByText(
        (_, node) => node?.textContent === "n***@example.com can use Gmail.",
      ),
    ).toBeTruthy();
    // The account this card's own sign-in brought in is sent as the pick at once.
    await waitFor(() => expect(onSelect.mock.calls).toEqual([["ngc_new"]]));
    // Once the chat stores that pick, the one fitting account needs no second picker.
    selected = "ngc_new";
    rerender(tree());
    expect(
      screen.queryByRole("button", { name: /n\*\*\*@example.com/ }),
    ).toBeNull();
    open.mockRestore();
    startAuthorize.mockReset();
  });
  it("shows a running Connected Gmail sign-in even when native is the card's default", () => {
    const flow: ConnectedGmailFlow = {
      status: "unavailable",
      canConnect: true,
      busy: true,
      failure: null,
      connections: [],
      start: vi.fn(),
    };
    render(
      <PageSlotsProvider
        value={{
          connectedGmailFlow: ({
            children,
          }: {
            children: (value: ConnectedGmailFlow) => React.ReactNode;
          }) => <>{children(flow)}</>,
        }}
      >
        <ConnectionCard
          card={{
            integrationId: "gmail",
            integrationName: "Gmail",
            provider: "google",
            scopeProfile: "gmail",
            requiredScopes: [GMAIL],
            reconnectConnectionId: null,
          }}
        />
      </PageSlotsProvider>,
    );

    const button = screen.getByRole("button", { name: "Waiting for sign-in…" });
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });
  it("keeps the start failure visible when a 409 meets a Connected Gmail flow that cannot connect", async () => {
    startAuthorize.mockReset();
    startAuthorize.mockRejectedValue({ response: { status: 409 } });
    const popup = {
      location: { assign: vi.fn() },
      close: vi.fn(),
      closed: false,
      opener: null,
    };
    const open = vi
      .spyOn(window, "open")
      .mockReturnValue(popup as unknown as Window);
    const start = vi.fn();
    const flow: ConnectedGmailFlow = {
      status: "unavailable",
      canConnect: false,
      busy: false,
      failure: null,
      connections: [],
      start,
    };
    render(
      <PageSlotsProvider
        value={{
          connectedGmailFlow: ({
            children,
          }: {
            children: (value: ConnectedGmailFlow) => React.ReactNode;
          }) => <>{children(flow)}</>,
        }}
      >
        <ConnectionCard
          card={{
            integrationId: "gmail",
            integrationName: "Gmail",
            provider: "google",
            scopeProfile: "gmail",
            requiredScopes: [GMAIL],
            reconnectConnectionId: null,
          }}
        />
      </PageSlotsProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    expect(
      await screen.findByText("Could not start the connection."),
    ).toBeTruthy();
    expect(start).not.toHaveBeenCalled();
    open.mockRestore();
    startAuthorize.mockReset();
  });
});
