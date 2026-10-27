import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  GoogleOAuthCredential,
  OrganizationPhoneNumber,
} from "@/api/types";
import type * as TwilioHooksModule from "@/hooks/useTwilioIntegration";
import type * as GoogleOAuthCredentialsModule from "@/hooks/useGoogleOAuthCredentials";
import { ActiveOrgContext } from "@/store/ActiveOrgContext";
import { Integrations } from "./Integrations";
import { TwilioPhoneNumberPicker } from "./twilio/TwilioPhoneNumberPicker";
import { TwilioIntegrationPanel } from "./twilio/TwilioIntegrationPanel";

const mocks = vi.hoisted(() => ({
  useGoogleOAuthCredentials: vi.fn(),
  startAuthorize: vi.fn(),
  assign: vi.fn(),
}));
// prettier-ignore
const state = vi.hoisted(() => ({ twilio: { apiKeySid: null as string | null, available: false, loading: false, fetching: false, error: false }, registry: { status: "disabled" as OrganizationPhoneNumber["status"], provider: "twilio", credentialId: "cred_existing", loading: false, fetching: false, error: false }, phone: { sid: "PN1", id: "pn_1", number: "+15551234567", enabled: false, loading: false, fetching: false, error: false, removed: false }, enable: { mutateAsync: vi.fn(), isPending: false }, disable: { mutateAsync: vi.fn(), isPending: false }, save: { mutate: vi.fn(), isPending: false }, delete: { mutate: vi.fn(), isPending: false }, useRealRegistry: false, useRealCredential: false, useRealEnable: false, useRealSave: false, client: { get: vi.fn(), post: vi.fn() }, toast: vi.fn(), makeTakeoverError: () => new Error() }));
vi.mock("@/hooks/useGoogleOAuthCredentials", async (importActual) => {
  const actual = await importActual<typeof GoogleOAuthCredentialsModule>();
  return {
    ...actual,
    useGoogleOAuthCredentials: mocks.useGoogleOAuthCredentials,
  };
});

// The client-config form issues its own network calls; it is irrelevant here.
vi.mock("@/components/GoogleOAuthClientConfigForm", () => ({
  GoogleOAuthClientConfigForm: () => null,
}));

// prettier-ignore
const query = <T,>(data: T, status: { loading: boolean; fetching: boolean; error: boolean }) => ({ data, isLoading: status.loading, isFetching: status.fetching, isError: status.error, error: status.error ? new Error() : null });
// prettier-ignore
vi.mock("@/hooks/useTwilioIntegration", async () => { const actual = await vi.importActual<typeof TwilioHooksModule>("@/hooks/useTwilioIntegration"); state.makeTakeoverError = () => new actual.TakeoverRequiredError({ current_sms_url: "https://foreign.example", current_sms_method: "POST", current_sms_application_sid: null }); return { ...actual, TakeoverRequiredError: actual.TakeoverRequiredError, useTwilioCredentialQuery: () => state.useRealCredential ? actual.useTwilioCredentialQuery() : query(state.twilio.available ? { available: true, credential: { account_sid: "AC1234", api_key_sid: state.twilio.apiKeySid, has_api_key_secret: !!state.twilio.apiKeySid, has_auth_token: true } } : { available: false, credential: null }, state.twilio), useRegisteredPhoneNumbersQuery: () => state.useRealRegistry ? actual.useRegisteredPhoneNumbersQuery() : query([{ phone_number_id: "pn_1", phone_number: state.phone.number, provider: state.registry.provider, status: state.registry.status, credential_id: state.registry.credentialId }], state.registry), useTwilioPhoneNumbersQuery: () => query(state.phone.removed ? [] : [{ sid: state.phone.sid, phone_number_id: state.phone.id, phone_number: state.phone.number, friendly_name: "Test", sms_capable: true, enabled: state.phone.enabled, current_sms_url: null }], state.phone), useEnablePhoneNumberMutation: () => state.useRealEnable ? actual.useEnablePhoneNumberMutation() : state.enable, useDisablePhoneNumberMutation: () => state.disable, useDeleteTwilioCredentialMutation: () => state.delete, useSaveTwilioCredentialMutation: () => state.useRealSave ? actual.useSaveTwilioCredentialMutation() : state.save }; });
// prettier-ignore
vi.mock("@/routes/workflows/components/CredentialCombobox", () => ({ CredentialCombobox: ({ onValueChange, disabled }: { onValueChange: (value: string, selection: unknown) => void; disabled?: boolean }) => <button type="button" disabled={disabled} onClick={() => onValueChange("__no_credential__", { type: "extra" })}>No credential</button> }));
// prettier-ignore
vi.mock("@/api/AxiosClient", () => ({ getClient: vi.fn(async () => state.client) }));
// prettier-ignore
vi.mock("@/hooks/useCredentialGetter", () => ({ useCredentialGetter: () => vi.fn() }));
// prettier-ignore
vi.mock("@/components/ui/use-toast", () => ({ useToast: () => ({ toast: state.toast }) }));
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

// prettier-ignore
const panel = (org = "org-a") => <ActiveOrgContext.Provider value={org}><TwilioIntegrationPanel /></ActiveOrgContext.Provider>;
const enable = () => screen.getByRole("switch", { name: /Enable/ });
const takeover = () => screen.getByRole("button", { name: "Replace webhook" });
// prettier-ignore
const takeoverDialog = () => screen.queryByRole("button", { name: "Replace webhook" });
// prettier-ignore
const blocked = (element: HTMLElement) => expect(element.getAttribute("disabled")).not.toBeNull();
// prettier-ignore
function blockedAction(view: { rerender: (ui: JSX.Element) => void }, status: { loading: boolean; fetching: boolean; error: boolean }, control: () => HTMLElement) { status.fetching = true; view.rerender(panel()); const element = control(); blocked(element); fireEvent.click(element); expect(state.enable.mutateAsync).not.toHaveBeenCalled(); status.fetching = false; view.rerender(panel()); }
// prettier-ignore
async function openTakeover() { state.enable.mutateAsync.mockRejectedValueOnce(state.makeTakeoverError()); fireEvent.click(enable()); await waitFor(() => expect(takeover()).toBeTruthy()); }
// prettier-ignore
const resetState = () => { Object.assign(state.twilio, { apiKeySid: null, available: false, loading: false, fetching: false, error: false }); Object.assign(state.registry, { status: "disabled", provider: "twilio", credentialId: "cred_existing", loading: false, fetching: false, error: false }); Object.assign(state.phone, { sid: "PN1", id: "pn_1", number: "+15551234567", enabled: false, loading: false, fetching: false, error: false, removed: false }); state.enable.isPending = state.disable.isPending = state.delete.isPending = state.save.isPending = false; state.save.mutate.mockReset(); state.useRealRegistry = state.useRealCredential = state.useRealEnable = state.useRealSave = false; state.enable.mutateAsync.mockReset(); state.disable.mutateAsync.mockReset(); state.delete.mutate.mockReset(); state.client.get.mockReset(); state.client.post.mockReset(); state.toast.mockReset(); };
beforeEach(() => {
  vi.clearAllMocks();
  resetState();
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
describe("Twilio integration panel", () => {
  // prettier-ignore
  it("keeps the saved Account SID and clears secrets after changing accounts", async () => {
    state.useRealCredential = state.useRealSave = true;
    const saved = { account_sid: "AC5678", api_key_sid: "SK5678", has_api_key_secret: true, has_auth_token: true };
    state.client.get.mockResolvedValueOnce({ data: { credential: { ...saved, account_sid: "AC1234" } } });
    state.client.get.mockResolvedValue({ data: { credential: saved } });
    state.client.post.mockResolvedValue({ data: { credential: saved } });
    render(<QueryClientProvider client={new QueryClient()}>{panel()}</QueryClientProvider>);
    fireEvent.click(await screen.findByRole("button", { name: "Update credentials" }));
    expect((screen.getByLabelText("Account SID") as HTMLInputElement).value).toBe("AC1234");
    const fields = { "Account SID": "AC5678", "API Key SID": "SK5678", "API Key Secret": "replacement-secret", "Auth Token": "replacement-token" };
    for (const [label, value] of Object.entries(fields)) {
      fireEvent.change(screen.getByLabelText(label), { target: { value } });
    }
    fireEvent.click(screen.getByRole("button", { name: "Save credentials" }));
    await waitFor(() => expect(state.client.post).toHaveBeenCalledWith(
      "/integrations/twilio/credentials",
      { credential: { account_sid: "AC5678", api_key_sid: "SK5678", api_key_secret: "replacement-secret", auth_token: "replacement-token" } },
    ));
    await waitFor(() => {
      expect(screen.getByText("AC••••5678")).toBeTruthy();
      for (const label of ["API Key SID", "API Key Secret", "Auth Token"]) {
        expect((screen.getByLabelText(label) as HTMLInputElement).value).toBe("");
      }
      expect((screen.getByLabelText("Account SID") as HTMLInputElement).value).toBe("AC5678");
    });
  });

  it("warns that updating credentials replaces a stored API key", () => {
    state.twilio.available = true;
    state.twilio.apiKeySid = "SK1234";
    render(panel());
    fireEvent.click(screen.getByRole("button", { name: "Update credentials" }));
    expect(
      screen.getByText(
        /This update replaces the stored credential.*enter the API Key SID and Secret again/i,
      ),
    ).toBeTruthy();
  });

  it("preserves entered credentials through a refresh after a failed save", async () => {
    state.twilio.available = true;
    state.useRealSave = true;
    state.client.post.mockRejectedValueOnce(new Error("Invalid credentials"));
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false } },
    });
    const wrapper = () => (
      <QueryClientProvider client={queryClient}>{panel()}</QueryClientProvider>
    );
    const view = render(wrapper());
    fireEvent.click(screen.getByRole("button", { name: "Update credentials" }));
    fireEvent.change(screen.getByLabelText("Auth Token"), {
      target: { value: "replacement-token" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save credentials" }));
    await waitFor(() =>
      expect(state.toast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Error",
          description: "Invalid credentials",
        }),
      ),
    );
    state.twilio.fetching = true;
    view.rerender(wrapper());
    state.twilio.fetching = false;
    view.rerender(wrapper());
    expect(
      (screen.getByLabelText("Auth Token") as HTMLInputElement).value,
    ).toBe("replacement-token");
  });

  it("gates the launcher and every stale action", () => {
    state.twilio.available = true;
    state.twilio.loading = true;
    const view = render(panel());
    expect(screen.getByText("Loading Twilio integration...")).toBeTruthy();
    state.twilio.loading = false;
    state.twilio.fetching = true;
    view.rerender(panel());
    blocked(enable());
    state.twilio.fetching = false;
    view.rerender(panel());
    for (const status of [state.phone, state.registry]) {
      blockedAction(view, status, enable);
    }
    state.enable.isPending = true;
    view.rerender(panel());
    blocked(enable());
    state.enable.isPending = false;
    state.phone.enabled = true;
    state.registry.status = "active";
    state.disable.isPending = true;
    view.rerender(panel());
    const disable = screen.getByRole("switch", { name: /Disable/ });
    blocked(disable);
    fireEvent.click(disable);
    expect(state.disable.mutateAsync).not.toHaveBeenCalled();
    state.phone.enabled = false;
    state.registry.status = "disabled";
    state.disable.isPending = false;
    state.twilio.error = true;
    view.rerender(panel());
    // prettier-ignore
    expect(screen.getByText("Could not load the Twilio integration. Try again.")).toBeTruthy();
    state.twilio.error = false;
    state.delete.isPending = true;
    view.rerender(panel());
    fireEvent.click(screen.getByRole("button", { name: "Disconnect" }));
    const confirm = screen
      .getAllByRole("button", { name: "Disconnect" })
      .pop()!;
    blocked(confirm);
    fireEvent.click(confirm);
    expect(state.delete.mutate).not.toHaveBeenCalled();
  });

  it("uses current takeover identity and binding", async () => {
    state.twilio.available = true;
    const view = render(panel());
    await openTakeover();
    state.registry.credentialId = "cred_new";
    view.rerender(panel());
    fireEvent.click(takeover());
    // prettier-ignore
    await waitFor(() => expect(state.enable.mutateAsync).toHaveBeenLastCalledWith({ phone_number_sid: "PN1", credential_id: "cred_new", confirm_takeover: true }));
    // prettier-ignore
    for (const change of [{ sid: "PN_CHANGED" }, { number: "+19999999999" }, { removed: true }]) { await openTakeover(); const before = state.enable.mutateAsync.mock.calls.length; const original = { ...state.phone }; Object.assign(state.phone, change); view.rerender(panel()); fireEvent.click(takeover()); await waitFor(() => expect(takeoverDialog()).toBeNull()); expect(state.enable.mutateAsync).toHaveBeenCalledTimes(before); Object.assign(state.phone, original); view.rerender(panel()); }
    await openTakeover();
    view.rerender(panel("org-b"));
    expect(takeoverDialog()).toBeNull();
    state.registry.credentialId = "cred_existing";
    view.rerender(panel("org-a"));
    fireEvent.click(enable());
    // prettier-ignore
    expect(state.enable.mutateAsync).toHaveBeenLastCalledWith({ phone_number_sid: "PN1", credential_id: "cred_existing" });
    fireEvent.click(screen.getByRole("button", { name: "No credential" }));
    fireEvent.click(enable());
    // prettier-ignore
    expect(state.enable.mutateAsync).toHaveBeenLastCalledWith({ phone_number_sid: "PN1", credential_id: null });
  });

  it("renders takeover after real-hook invalidation", async () => {
    state.useRealCredential = state.useRealEnable = true;
    // prettier-ignore
    void (state.client.get.mockResolvedValue({ data: { credential: { account_sid: "AC1234", api_key_sid: null, has_auth_token: true } } }), state.client.post.mockRejectedValueOnce(Object.assign(new Error("conflict"), { isAxiosError: true, response: { status: 409, data: { detail: { current_sms_url: "https://foreign.example", current_sms_method: "POST", current_sms_application_sid: null } } } })));
    render(
      <QueryClientProvider client={new QueryClient()}>
        {panel()}
      </QueryClientProvider>,
    );
    await waitFor(() => expect(enable()).toBeTruthy());
    fireEvent.click(enable());
    await waitFor(() => expect(takeover()).toBeTruthy());
  });
});

describe("Twilio recovery", () => {
  it.each(["active", "quarantined"] as const)(
    "disables %s rows with drifted or missing provider numbers",
    (status) => {
      state.twilio.available = true;
      state.registry.status = status;
      const view = render(panel());
      for (const removed of [false, true]) {
        state.phone.removed = removed;
        view.rerender(panel());
        fireEvent.click(screen.getByRole("button", { name: /Disable/ }));
        expect(state.disable.mutateAsync).toHaveBeenCalledWith({
          phone_number_id: "pn_1",
        });
        state.disable.mutateAsync.mockClear();
        expect(state.enable.mutateAsync).not.toHaveBeenCalled();
      }
    },
  );

  it("replaces revoked credentials and disconnects without provider inventory", async () => {
    state.twilio.available = state.phone.error = true;
    const view = render(panel());
    fireEvent.click(screen.getByRole("button", { name: "Update credentials" }));
    fireEvent.change(screen.getByLabelText("Auth Token"), {
      target: { value: "replacement-token" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save credentials" }));
    await waitFor(() =>
      expect(state.save.mutate.mock.calls[0]?.[0]).toMatchObject({
        account_sid: "AC1234",
        auth_token: "replacement-token",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Disconnect" }));
    const confirm = () =>
      screen.getAllByRole("button", { name: "Disconnect" }).pop()!;
    for (const status of ["active", "quarantined"] as const) {
      state.registry.status = status;
      view.rerender(panel());
      blocked(confirm());
    }
    state.registry.status = "disabled";
    state.registry.error = true;
    view.rerender(panel());
    blocked(confirm());
    state.registry.error = false;
    view.rerender(panel());
    fireEvent.click(confirm());
    expect(state.delete.mutate).toHaveBeenCalledTimes(1);
  });
});

describe("SMS registry picker", () => {
  it.each([404, 500, undefined])(
    "handles registry status %s",
    async (status) => {
      state.useRealRegistry = true;
      state.client.get.mockRejectedValue(
        Object.assign(new Error("request failed"), {
          isAxiosError: true,
          response: status ? { status } : undefined,
        }),
      );
      const client = new QueryClient({
        defaultOptions: { queries: { retry: 2, retryDelay: 0 } },
      });
      render(
        <QueryClientProvider client={client}>
          <TwilioPhoneNumberPicker value={null} onChange={vi.fn()} />
        </QueryClientProvider>,
      );
      expect(
        await screen.findByText(
          status === 404
            ? /No SMS 2FA number is connected/
            : /Could not load registered SMS 2FA numbers/,
        ),
      ).toBeTruthy();
      expect(state.client.get).toHaveBeenCalledTimes(status === 404 ? 1 : 3);
      if (status === 404) {
        expect(
          screen.queryByText(/Could not load registered SMS 2FA numbers/),
        ).toBeNull();
      }
      client.clear();
    },
  );
});
