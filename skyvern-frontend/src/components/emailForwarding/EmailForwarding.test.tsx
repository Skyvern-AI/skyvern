import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import {
  focusManager,
  onlineManager,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ReactNode, useState } from "react";
import CloudContext from "@/store/CloudContext";
import { PageSlotsProvider } from "@/store/PageSlots";
import {
  useEmailForwardingAvailable,
  type ForwardingCapability,
} from "@/hooks/useEmailForwarding";
import { forwardingError, type ForwardingAddress } from "@/api/emailForwarding";
import { CredentialForwardingAddress } from "./CredentialForwardingAddress";
import { ForwardingInbox } from "./ForwardingInbox";
import { ForwardingAddressPanel } from "./ForwardingAddressPanel";
import { EmailForwardingSettings } from "./EmailForwardingSettings";

const api = vi.hoisted(() => ({
  get: vi.fn(),
  getAvailability: vi.fn(),
  post: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("@/api/AxiosClient", () => ({
  getClient: async (_credentialGetter: unknown, apiVersion: string) => ({
    get: (url: string, ...args: unknown[]) => {
      if (url.endsWith("/availability")) {
        const basePath = apiVersion === "v1" ? "/api/v1" : "/v1";
        return api.getAvailability(`${basePath}${url}`, ...args);
      }
      return api.get(url, ...args);
    },
    post: api.post,
    delete: api.delete,
  }),
}));

const address: ForwardingAddress = {
  forwarding_address_id: "fea_test",
  organization_id: "o_test",
  email_config_id: "emc_test",
  email_address: "email-test-abc123@email.example",
  org_slug: "test",
  match_mode: "dedicated",
  credential_id: "cred_test",
  sender_allowlist: null,
  status: "pending",
  last_received_at: null,
  has_pending_confirmation: true,
  created_at: "2026-09-01T00:00:00",
  modified_at: "2026-09-01T00:00:00",
};

const forwardingActions = [
  "list",
  "read",
  "create",
  "update",
  "delete",
] as const;

function mount(
  children: ReactNode,
  isCloud = true,
  initialEntries: string[] = ["/"],
) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, retryDelay: 0 },
      mutations: { retry: false },
    },
  });
  const result = render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={initialEntries}>
        <CloudContext.Provider value={isCloud}>
          {children}
        </CloudContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...result, client };
}

function mountWithForwardingAllowed(
  children: ReactNode,
  allowed: boolean | Partial<Record<ForwardingCapability, boolean>>,
  permissionStatus: "ready" | "loading" = "ready",
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const capabilities = forwardingActions.map((action) => ({
    capability: `email_forwarding.${action}`,
    allowed: typeof allowed === "boolean" ? allowed : (allowed[action] ?? true),
    reason: "role" as const,
  }));
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <CloudContext.Provider value={true}>
          <PageSlotsProvider
            value={{
              permissions:
                permissionStatus === "ready"
                  ? {
                      status: "ready",
                      data: {
                        organization_id: "o_test",
                        user_id: "u_1",
                        auth_kind: "bearer",
                        org_role: "org:member",
                        role: "member",
                        capabilities,
                      },
                    }
                  : { status: "loading" },
            }}
          >
            {children}
          </PageSlotsProvider>
        </CloudContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  focusManager.setFocused(undefined);
});
beforeEach(() => {
  vi.resetAllMocks();
  api.getAvailability.mockResolvedValue({ data: { available: true } });
  api.get.mockResolvedValue({ data: [] });
});

function AvailabilityProbe() {
  const available = useEmailForwardingAvailable();
  return <span>{available ? "Available" : "Unavailable"}</span>;
}

function SettingsNavigation() {
  const [visible, setVisible] = useState(true);
  return (
    <>
      <button onClick={() => setVisible(!visible)}>
        {visible ? "Leave settings" : "Return to settings"}
      </button>
      {visible && <EmailForwardingSettings />}
    </>
  );
}

describe("email forwarding", () => {
  it("uses a structured role-denial message instead of generic update copy", () => {
    expect(
      forwardingError({
        isAxiosError: true,
        response: {
          status: 403,
          data: {
            detail: {
              error_code: "organization_admin_required",
              message: "Your role (Developer) can't do this.",
            },
          },
        },
      }),
    ).toBe("Your role (Developer) can't do this.");
  });

  it("loads server availability once and shows forwarding settings when available", async () => {
    const original = Element.prototype.scrollIntoView;
    const scrolled: string[] = [];
    Element.prototype.scrollIntoView = function () {
      scrolled.push(this.id);
    };
    try {
      mount(<EmailForwardingSettings />, true, ["/settings#email-forwarding"]);
      await waitFor(() => expect(scrolled).toEqual(["email-forwarding"]));
      await screen.findByRole("button", { name: "Generate address" });
      expect(api.getAvailability).toHaveBeenCalledTimes(1);
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });

  it("hides settings and makes no forwarding request when unavailable", async () => {
    api.getAvailability.mockResolvedValue({ data: { available: false } });
    mount(<EmailForwardingSettings />);

    await waitFor(() => expect(api.getAvailability).toHaveBeenCalledTimes(1));
    expect(
      screen.queryByRole("button", { name: "Generate address" }),
    ).toBeNull();
    expect(api.get).not.toHaveBeenCalled();
  });

  it.each([
    ["a network failure", { isAxiosError: true }],
    ["a server error", { isAxiosError: true, response: { status: 503 } }],
  ])(
    "shows settings after %s is followed by a successful request",
    async (_, error) => {
      api.getAvailability.mockRejectedValueOnce(error);
      mount(<EmailForwardingSettings />);

      expect(
        screen.queryByRole("button", { name: "Generate address" }),
      ).toBeNull();
      expect(
        await screen.findByRole("button", { name: "Generate address" }),
      ).toBeTruthy();
    },
  );

  it("recovers on remount after transient retries are exhausted", async () => {
    api.getAvailability.mockRejectedValue({
      isAxiosError: true,
      response: { status: 503 },
    });
    const { client } = mount(<SettingsNavigation />);
    await waitFor(() =>
      expect(
        client.getQueryState(["emailForwarding", undefined, "availability"])
          ?.status,
      ).toBe("error"),
    );
    expect(
      screen.queryByRole("button", { name: "Generate address" }),
    ).toBeNull();

    api.getAvailability.mockResolvedValue({ data: { available: true } });
    fireEvent.click(screen.getByRole("button", { name: "Leave settings" }));
    fireEvent.click(screen.getByRole("button", { name: "Return to settings" }));
    expect(
      await screen.findByRole("button", { name: "Generate address" }),
    ).toBeTruthy();
  });

  it.each([
    [
      "role denial",
      {
        status: 403,
        data: { detail: { error_code: "organization_admin_required" } },
      },
    ],
    [
      "billing role denial",
      {
        status: 403,
        data: {
          detail: {
            error_code: "billing_admin_not_allowed",
            reason_kind: "role",
          },
        },
      },
    ],
    ["missing route", { status: 404 }],
  ])(
    "keeps settings hidden and does not retry an unavailable response (%s)",
    async (_, response) => {
      api.getAvailability.mockRejectedValueOnce({
        isAxiosError: true,
        response,
      });
      mount(<SettingsNavigation />);

      await waitFor(() => expect(api.getAvailability).toHaveBeenCalledTimes(1));
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 50));
        focusManager.setFocused(false);
        focusManager.setFocused(true);
        await new Promise((resolve) => setTimeout(resolve, 50));
      });
      fireEvent.click(screen.getByRole("button", { name: "Leave settings" }));
      fireEvent.click(
        screen.getByRole("button", { name: "Return to settings" }),
      );
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 50));
      });
      expect(
        screen.queryByRole("button", { name: "Generate address" }),
      ).toBeNull();
      expect(api.getAvailability).toHaveBeenCalledTimes(1);
      expect(api.get).not.toHaveBeenCalled();
    },
  );

  it("fails closed on availability errors and does not request forwarding data", async () => {
    api.getAvailability.mockRejectedValue(new Error("Route unavailable"));
    mount(<AvailabilityProbe />);

    await screen.findByText("Unavailable");
    await waitFor(() => expect(api.getAvailability).toHaveBeenCalledTimes(1));
    expect(api.get).not.toHaveBeenCalled();
  });

  it("requests availability from the cloud /api/v1 route", async () => {
    mount(<AvailabilityProbe />);

    expect(await screen.findByText("Available")).toBeTruthy();
    expect(api.getAvailability).toHaveBeenCalledWith(
      "/api/v1/email-forwarding/availability",
    );
  });

  it("exposes forwarding in OSS without calling the cloud availability route", async () => {
    mount(
      <>
        <AvailabilityProbe />
        <EmailForwardingSettings />
        <CredentialForwardingAddress
          credentialId="cred_test"
          identifier=""
          onChange={vi.fn()}
        />
      </>,
      false,
    );

    expect(await screen.findByText("Available")).toBeTruthy();
    expect(
      await screen.findByText(/Bring your own email receiver/),
    ).toBeTruthy();
    expect(
      screen.queryByRole("button", {
        name: "Generate a Skyvern forwarding address",
      }),
    ).toBeNull();
    expect(
      await screen.findByText(
        /For self-hosted forwarding, register a receiver/,
      ),
    ).toBeTruthy();
    expect(api.getAvailability).not.toHaveBeenCalled();
    expect(api.get).toHaveBeenCalledWith("/email-forwarding/addresses");
    expect(api.post).not.toHaveBeenCalled();
  });

  it("shows OSS registration guidance before a credential is saved", () => {
    mount(
      <CredentialForwardingAddress identifier="" onChange={vi.fn()} />,
      false,
    );

    expect(
      screen.getByText(/For self-hosted forwarding, register a receiver/),
    ).toBeTruthy();
    expect(api.getAvailability).not.toHaveBeenCalled();
    expect(api.get).not.toHaveBeenCalled();
  });

  it("binds a generated address to the saved credential and updates its email identifier", async () => {
    let created = false;
    api.get.mockImplementation(async () => ({
      data: created ? [address] : [],
    }));
    api.post.mockImplementation(async () => {
      created = true;
      return { data: address };
    });
    const onChange = vi.fn();
    function CredentialForm() {
      const [identifier, setIdentifier] = useState("");
      return (
        <CredentialForwardingAddress
          credentialId="cred_test"
          identifier={identifier}
          onChange={(value) => {
            onChange(value);
            setIdentifier(value);
          }}
        />
      );
    }
    mount(<CredentialForm />);
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Generate a Skyvern forwarding address",
      }),
    );
    await waitFor(() =>
      expect(onChange).toHaveBeenCalledWith(address.email_address),
    );
    expect(api.post).toHaveBeenCalledWith(
      "/email-forwarding/addresses/managed",
      {
        credential_id: "cred_test",
        match_mode: "dedicated",
      },
    );
    expect(await screen.findByDisplayValue(address.email_address)).toBeTruthy();
  });

  it("offers generation after revoking a stale address when the saved binding is clear", async () => {
    let revoked = false;
    api.get.mockImplementation(async () => ({
      data: revoked ? [] : [address],
    }));
    api.delete.mockImplementation(async () => {
      revoked = true;
      return { data: null };
    });
    mount(
      <CredentialForwardingAddress
        credentialId="cred_test"
        identifier="changed@example.test"
        savedTotpType="none"
        onChange={vi.fn()}
      />,
    );
    await screen.findByText("Previously generated address");
    expect(
      screen.queryByRole("button", {
        name: "Generate a Skyvern forwarding address",
      }),
    ).toBeNull();
    expect(screen.queryByLabelText("Forwarding address")).toBeNull();
    fireEvent.click(
      screen.getByRole("button", { name: "Revoke previous address" }),
    );
    expect(api.delete).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Revoke address" }));
    expect(
      await screen.findByRole("button", {
        name: "Generate a Skyvern forwarding address",
      }),
    ).toBeTruthy();
    expect(api.delete).toHaveBeenCalledWith(
      "/email-forwarding/addresses/fea_test",
    );
  });

  it.each([
    ["denied", "ready"],
    ["loading", "loading"],
  ] as const)(
    "sends no forwarding request and shows no surface while capabilities are %s",
    async (_state, permissionStatus) => {
      mountWithForwardingAllowed(
        <>
          <EmailForwardingSettings />
          <CredentialForwardingAddress
            credentialId="cred_test"
            identifier="user@example.test"
            onChange={vi.fn()}
          />
        </>,
        false,
        permissionStatus,
      );
      // Let React Query settle so the cold-cache request assertion is meaningful.
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 50));
      });
      expect(api.getAvailability).not.toHaveBeenCalled();
      expect(api.get).not.toHaveBeenCalled();
      expect(screen.queryByText("Email forwarding")).toBeNull();
      expect(
        screen.queryByRole("button", {
          name: "Generate a Skyvern forwarding address",
        }),
      ).toBeNull();
      expect(api.post).not.toHaveBeenCalled();
    },
  );

  it("closes the inbox and hides cached messages when read access is revoked", async () => {
    let revokeRead = () => {};
    function CapabilityHarness({ children }: { children: ReactNode }) {
      const [canRead, setCanRead] = useState(true);
      revokeRead = () => setCanRead(false);
      const capabilities = (
        ["list", "read", "create", "update", "delete"] as const
      ).map((action) => ({
        capability: `email_forwarding.${action}`,
        allowed: action === "read" ? canRead : true,
        reason: "role" as const,
      }));
      return (
        <PageSlotsProvider
          value={{
            permissions: {
              status: "ready",
              data: {
                organization_id: "o_test",
                user_id: "u_1",
                auth_kind: "bearer",
                org_role: "org:member",
                role: "member",
                capabilities,
              },
            },
          }}
        >
          {children}
        </PageSlotsProvider>
      );
    }

    api.get.mockResolvedValue({
      data: [
        {
          inbound_email_id: "email_test",
          category: "otp",
          subject: "Verification code",
          from_address: "sender@example.test",
          created_at: "2026-10-01T00:00:00",
          body_text: "Cached verification message",
          links: [],
          verdicts: {},
        },
      ],
      headers: { "x-has-more": "false" },
    });
    mount(
      <CapabilityHarness>
        <ForwardingAddressPanel address={address} />
      </CapabilityHarness>,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "View confirmation" }),
    );
    expect(await screen.findByText("Cached verification message")).toBeTruthy();

    act(() => revokeRead());
    await waitFor(() => {
      expect(screen.queryByText("Cached verification message")).toBeNull();
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });

  it("still lists addresses for a role that holds the capability", async () => {
    api.get.mockImplementation(async () => ({ data: [address] }));
    mountWithForwardingAllowed(<EmailForwardingSettings />, true);
    await waitFor(() =>
      expect(api.get).toHaveBeenCalledWith("/email-forwarding/addresses"),
    );
    expect(await screen.findByText("Email forwarding")).toBeTruthy();
  });

  it("checks availability and lists addresses for a list-only role", async () => {
    mountWithForwardingAllowed(<EmailForwardingSettings />, {
      list: true,
      read: false,
      create: false,
      update: false,
      delete: false,
    });

    expect(await screen.findByText("Email forwarding")).toBeTruthy();
    expect(api.getAvailability).toHaveBeenCalledWith(
      "/api/v1/email-forwarding/availability",
    );
    await waitFor(() =>
      expect(api.get).toHaveBeenCalledWith("/email-forwarding/addresses"),
    );
    expect(
      screen.queryByRole("button", { name: "Generate address" }),
    ).toBeNull();
  });

  it.each([
    {
      capability: "create",
      launchButton: "Generate address",
      confirmButton: "Create address",
    },
    {
      capability: "update",
      launchButton: "Rotate",
      confirmButton: "Rotate address",
    },
    {
      capability: "delete",
      launchButton: "Revoke",
      confirmButton: "Revoke address",
    },
  ] as const)(
    "closes the $capability confirmation when access is revoked",
    async ({ capability, launchButton, confirmButton }) => {
      let revokeCapability: () => void = () => {};
      function CapabilityHarness({ children }: { children: ReactNode }) {
        const [allowed, setAllowed] = useState<
          Record<ForwardingCapability, boolean>
        >({ list: true, read: true, create: true, update: true, delete: true });
        revokeCapability = () =>
          setAllowed((current) => ({ ...current, [capability]: false }));
        const capabilities = forwardingActions.map((action) => ({
          capability: `email_forwarding.${action}`,
          allowed: allowed[action],
          reason: "role" as const,
        }));
        return (
          <PageSlotsProvider
            value={{
              permissions: {
                status: "ready",
                data: {
                  organization_id: "o_test",
                  user_id: "u_1",
                  auth_kind: "bearer",
                  org_role: "org:member",
                  role: "member",
                  capabilities,
                },
              },
            }}
          >
            {children}
          </PageSlotsProvider>
        );
      }

      api.get.mockResolvedValue({ data: [address] });
      mount(
        <CapabilityHarness>
          <EmailForwardingSettings />
        </CapabilityHarness>,
      );
      fireEvent.click(
        await screen.findByRole("button", { name: launchButton }),
      );
      expect(
        await screen.findByRole("button", { name: confirmButton }),
      ).toBeTruthy();

      act(() => revokeCapability());
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      expect(api.post).not.toHaveBeenCalled();
      expect(api.delete).not.toHaveBeenCalled();
    },
  );

  it("closes the previous-address revoke dialog when delete access is revoked", async () => {
    let revokeDelete = () => {};
    function CapabilityHarness({ children }: { children: ReactNode }) {
      const [canDelete, setCanDelete] = useState(true);
      revokeDelete = () => setCanDelete(false);
      const capabilities = forwardingActions.map((action) => ({
        capability: `email_forwarding.${action}`,
        allowed: action === "delete" ? canDelete : true,
        reason: "role" as const,
      }));
      return (
        <PageSlotsProvider
          value={{
            permissions: {
              status: "ready",
              data: {
                organization_id: "o_test",
                user_id: "u_1",
                auth_kind: "bearer",
                org_role: "org:member",
                role: "member",
                capabilities,
              },
            },
          }}
        >
          {children}
        </PageSlotsProvider>
      );
    }

    api.get.mockResolvedValue({ data: [address] });
    mount(
      <CapabilityHarness>
        <CredentialForwardingAddress
          credentialId="cred_test"
          identifier="changed@example.test"
          onChange={vi.fn()}
        />
      </CapabilityHarness>,
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Revoke previous address" }),
    );
    expect(await screen.findByRole("dialog")).toBeTruthy();

    act(() => revokeDelete());
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.delete).not.toHaveBeenCalled();
  });

  it("requires a saved credential before generating a bound address", async () => {
    mountWithForwardingAllowed(
      <CredentialForwardingAddress identifier="" onChange={vi.fn()} />,
      true,
    );
    expect(
      await screen.findByText(
        /click Email again to clear it, then save this credential/i,
      ),
    ).toBeTruthy();
    expect(
      screen.queryByRole("button", {
        name: "Generate a Skyvern forwarding address",
      }),
    ).toBeNull();
  });

  it("shows a backend entitlement denial without inventing a forwarding address", async () => {
    api.get.mockRejectedValue({
      isAxiosError: true,
      response: {
        status: 403,
        data: { detail: "Email 2FA forwarding requires the Pro plan." },
      },
    });
    mount(<EmailForwardingSettings />);
    expect(
      await screen.findByText("Email 2FA forwarding requires the Pro plan."),
    ).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Generate address" }),
    ).toBeNull();
  });

  it("requires clearing a saved two-factor method before generating", async () => {
    mount(
      <CredentialForwardingAddress
        credentialId="cred_test"
        identifier="user@example.test"
        savedTotpType="email"
        onChange={vi.fn()}
      />,
    );

    expect(
      await screen.findByText(
        /turn off the current two-factor method and save/i,
      ),
    ).toBeTruthy();
    expect(
      screen.queryByRole("button", {
        name: "Generate a Skyvern forwarding address",
      }),
    ).toBeNull();
  });

  it("shows the backend binding conflict when minting is rejected", async () => {
    api.post.mockRejectedValue({
      isAxiosError: true,
      response: {
        status: 409,
        data: { detail: "Credential already has a different TOTP binding" },
      },
    });
    mount(
      <CredentialForwardingAddress
        credentialId="cred_test"
        identifier="user@example.test"
        savedTotpType="none"
        onChange={vi.fn()}
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", {
        name: "Generate a Skyvern forwarding address",
      }),
    );

    expect((await screen.findByRole("alert")).textContent).toContain(
      "This credential already has a two-factor method. To switch to forwarding, remove that method, save the credential, then open it again to generate an address.",
    );
  });

  it("renders confirmation text and full safe URLs without HTML or automatic navigation", async () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    api.get.mockResolvedValue({
      data: [
        {
          inbound_email_id: "ibe_test",
          from_address: "forwarding-noreply@google.com",
          subject: "Forwarding confirmation",
          body_text:
            "Confirmation code: 123456789 <img src=x onerror=alert(1)>",
          category: "provider_confirmation",
          verdicts: { dmarcverdict: "PASS" },
          created_at: "2026-09-01T00:00:00",
          links: [
            {
              url: "https://mail-settings.google.com/mail/vf-fixture",
              text: "Confirm",
            },
            { url: "javascript:alert(1)", text: "Unsafe" },
          ],
        },
      ],
    });
    mount(<ForwardingInbox addressId="fea_test" />);
    const link = await screen.findByRole("link", {
      name: "https://mail-settings.google.com/mail/vf-fixture",
    });
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
    expect(
      screen.getByRole("button", { name: "Copy confirmation code 123456789" }),
    ).toBeTruthy();
    expect(document.querySelector("img")).toBeNull();
    expect(
      screen.queryByRole("link", { name: "javascript:alert(1)" }),
    ).toBeNull();
    expect(open).not.toHaveBeenCalled();
    open.mockRestore();
  });

  it.each([
    ["https://attacker.example/confirm", null],
    ["https://mail.google.com/mail/verify?token=fixture", undefined],
    ["https://mail.google.com/mail/verify?token=fixture", null],
    [
      "https://mail.google.com/mail/verify?token=fixture",
      { dmarcverdict: "GRAY" },
    ],
    [
      "https://mail.google.com/mail/verify?token=fixture",
      { dmarcverdict: "FAIL" },
    ],
  ])(
    "does not offer an untrusted provider confirmation link: %s",
    async (url, verdicts) => {
      api.get.mockResolvedValue({
        data: [
          {
            inbound_email_id: "ibe_forged",
            from_address: "forwarding-noreply@google.com",
            subject: "Forwarding confirmation",
            body_text: "Confirmation code: 123456789",
            category: "provider_confirmation",
            created_at: "2026-09-01T00:00:00",
            links: [{ url, text: "Confirm" }],
            verdicts,
          },
        ],
      });
      mount(<ForwardingInbox addressId="fea_test" />);
      await screen.findByText(/Unverified confirmation link/);
      expect(screen.queryByRole("link")).toBeNull();
      expect(
        screen.queryByText("Confirm your forwarding destination"),
      ).toBeNull();
      expect(
        screen.queryByRole("button", {
          name: "Copy confirmation code 123456789",
        }),
      ).toBeNull();
      if (verdicts?.dmarcverdict === "FAIL") {
        expect(screen.getByRole("alert").textContent).toBe(
          "This message failed sender authentication. Do not use it to confirm forwarding.",
        );
      } else {
        expect(
          screen.getByText(
            "Sender authentication could not be verified. Confirm forwarding from your email provider’s settings instead of using this message.",
          ),
        ).toBeTruthy();
      }
    },
  );

  it("keeps the code available but rejects a lookalike Gmail confirmation host", async () => {
    api.get.mockResolvedValue({
      data: [
        {
          inbound_email_id: "ibe_lookalike",
          from_address: "forwarding-noreply@google.com",
          subject: "Forwarding confirmation",
          body_text: "Confirmation code: 123456789",
          category: "provider_confirmation",
          verdicts: { dmarcverdict: "PASS" },
          created_at: "2026-09-01T00:00:00Z",
          links: [
            {
              url: "https://mail-settings.google.com.attacker.example/mail/vf-fixture",
              text: "Confirm",
            },
          ],
        },
      ],
    });
    mount(<ForwardingInbox addressId="fea_test" />);

    expect(
      await screen.findByText("Confirm your forwarding destination"),
    ).toBeTruthy();
    expect(screen.getByText(/Unverified confirmation link/)).toBeTruthy();
    expect(screen.queryByRole("link")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Copy confirmation code 123456789" }),
    ).toBeTruthy();
  });

  it("shows the browser-normalized host instead of an internationalized lookalike", async () => {
    const url = "https://gооgle.com/verify";
    api.get.mockResolvedValue({
      data: [
        {
          inbound_email_id: "ibe_link",
          from_address: "security@example.test",
          subject: "Sign in",
          body_text: "Review this link",
          category: "otp",
          created_at: "2026-09-01T00:00:00",
          links: [{ url, text: "Google" }],
        },
      ],
    });
    mount(<ForwardingInbox addressId="fea_test" />);
    const link = await screen.findByRole("link", { name: new URL(url).href });
    expect(link.getAttribute("href")).toBe(new URL(url).href);
    expect(link.textContent).toContain("xn--");
  });

  it.each(["dropped_spam", "dropped_other"])(
    "keeps links in %s messages as text",
    async (category) => {
      api.get.mockResolvedValue({
        data: [
          {
            inbound_email_id: "ibe_dropped",
            from_address: "forwarding-noreply@google.com",
            subject: "Dropped message",
            body_text: "Confirmation code: 123456789",
            category,
            created_at: "2026-09-01T00:00:00",
            links: [
              { url: "https://attacker.example/confirm", text: "Confirm" },
              {
                url: "https://mail.google.com/mail/verify?token=fixture",
                text: "Confirm",
              },
            ],
          },
        ],
      });
      mount(<ForwardingInbox addressId="fea_test" />);
      await screen.findByText("Dropped message");
      expect(screen.queryAllByRole("link")).toHaveLength(0);
      expect(
        screen.queryByRole("button", { name: /Copy confirmation code/ }),
      ).toBeNull();
      expect(
        screen.getByText(/https:\/\/attacker.example\/confirm/),
      ).toBeTruthy();
    },
  );

  it.each(["cred_test", null])(
    "shows an address with eligible credential binding %s",
    async (credential_id) => {
      api.get.mockResolvedValue({ data: [{ ...address, credential_id }] });
      mount(
        <CredentialForwardingAddress
          credentialId="cred_test"
          identifier={address.email_address}
          onChange={vi.fn()}
        />,
      );
      expect(
        await screen.findByDisplayValue(address.email_address),
      ).toBeTruthy();
    },
  );

  it("does not present another credential's forwarding address as current", async () => {
    api.get.mockResolvedValue({ data: [address] });
    mount(
      <CredentialForwardingAddress
        credentialId="cred_other"
        identifier={address.email_address}
        onChange={vi.fn()}
      />,
    );
    expect((await screen.findByRole("alert")).textContent).toContain(
      "another credential",
    );
    expect(screen.queryByDisplayValue(address.email_address)).toBeNull();
    expect(
      screen.queryByRole("button", {
        name: "Generate a Skyvern forwarding address",
      }),
    ).toBeNull();
  });

  it("does not match original-recipient addresses and allows mint when saved 2FA is clear", async () => {
    api.get.mockResolvedValue({
      data: [{ ...address, match_mode: "original_recipient" }],
    });
    mount(
      <CredentialForwardingAddress
        credentialId="cred_test"
        identifier={address.email_address}
        savedTotpType="none"
        onChange={vi.fn()}
      />,
    );

    expect(
      await screen.findByRole("button", {
        name: "Generate a Skyvern forwarding address",
      }),
    ).toBeTruthy();
    expect(screen.queryByDisplayValue(address.email_address)).toBeNull();
    expect(screen.queryByText("Previously generated address")).toBeNull();
  });

  it("renders a full inbox page before searching a long hidden tail", async () => {
    const email = (id: string, subject: string) => ({
      inbound_email_id: id,
      from_address: "otp@provider.test",
      subject,
      body_text: "Your code is 123456",
      links: [],
      category: "otp",
      created_at: "2026-08-01T00:00:00",
    });
    const offsets: string[] = [];
    api.get.mockImplementation(
      async (_url: string, options: { params: { offset: string } }) => {
        const offset = options.params.offset;
        offsets.push(offset);
        if (offset === "0") {
          return {
            data: Array.from({ length: 20 }, (_, i) =>
              email(`first-${i}`, `First email ${i}`),
            ),
            headers: { "x-has-more": "true", "x-next-cursor": "200" },
          };
        }
        if (["200", "400", "600"].includes(offset)) {
          return {
            data: [],
            headers: {
              "x-has-more": "true",
              "x-next-cursor": String(Number(offset) + 200),
            },
          };
        }
        if (offset === "800") {
          return {
            data: [email("last", "Last email")],
            headers: { "x-has-more": "false" },
          };
        }
        throw new Error(`Unexpected offset: ${offset}`);
      },
    );
    mount(<ForwardingInbox addressId="fea_test" />);

    await screen.findByText("First email 0");
    expect(offsets).toEqual(["0"]);
    fireEvent.click(screen.getByRole("button", { name: "Older" }));
    await screen.findByRole("button", { name: "Continue searching" });
    expect(offsets).toEqual(["0", "200", "400", "600"]);
    expect(screen.getByText("First email 0")).toBeTruthy();
    expect(screen.queryByText(/No received emails/)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Continue searching" }));
    await screen.findByText("Last email");
    expect(screen.getByText("Page 2")).toBeTruthy();
    expect(
      screen
        .getByRole("button", { name: "Continue searching" })
        .hasAttribute("disabled"),
    ).toBe(true);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Newer" }).hasAttribute("disabled"),
      ).toBe(false),
    );
    fireEvent.click(screen.getByRole("button", { name: "Newer" }));
    await screen.findByText("First email 0");
  });

  it("keeps later-window results when opening a searched inbox page", async () => {
    let accessChanged = false;
    const email = (id: string, subject: string) => ({
      inbound_email_id: id,
      from_address: "otp@provider.test",
      subject,
      body_text: "Your code is 123456",
      links: [],
      category: "otp",
      created_at: "2026-08-01T00:00:00",
    });
    api.get.mockImplementation(
      async (_url: string, options: { params: { offset: string } }) => {
        const offset = options.params.offset;
        if (offset === "0") {
          return {
            data: Array.from({ length: 20 }, (_, i) =>
              email(`first-${i}`, `First email ${i}`),
            ),
            headers: { "x-has-more": "true", "x-next-cursor": "200" },
          };
        }
        if (offset === "200") {
          return {
            data: [],
            headers: { "x-has-more": "true", "x-next-cursor": "400" },
          };
        }
        if (offset === "400") {
          if (accessChanged) {
            return {
              data: [email("new-access", "Newly accessible search result")],
              headers: { "x-has-more": "false" },
            };
          }
          return {
            data: [email("older-1", "Older first")],
            headers: { "x-has-more": "true", "x-next-cursor": "600" },
          };
        }
        return {
          data: [email("older-2", "Older second")],
          headers: { "x-has-more": "false" },
        };
      },
    );
    mount(<ForwardingInbox addressId="fea_test" />);
    await screen.findByText("First email 0");
    fireEvent.click(screen.getByRole("button", { name: "Older" }));
    await screen.findByText("Older first");
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Newer" }).hasAttribute("disabled"),
      ).toBe(false),
    );
    expect(screen.getByText("Older second")).toBeTruthy();
    accessChanged = true;
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await screen.findByText("First email 0");
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Older" }).hasAttribute("disabled"),
      ).toBe(false),
    );
    fireEvent.click(screen.getByRole("button", { name: "Older" }));
    await screen.findByText("Newly accessible search result");
    expect(screen.queryByText("Older first")).toBeNull();
  });

  it("polls the newest window without repeating continuation scans", async () => {
    vi.useFakeTimers();
    const offsets: string[] = [];
    api.get.mockImplementation(
      async (_url: string, options: { params: { offset: string } }) => {
        const offset = options.params.offset;
        offsets.push(offset);
        if (offset === "600") {
          return {
            data: [
              {
                inbound_email_id: "older",
                from_address: "otp@provider.test",
                subject: "Older after poll",
                body_text: "Your code is 123456",
                links: [],
                category: "otp",
                created_at: "2026-08-01T00:00:00",
              },
            ],
            headers: { "x-has-more": "false" },
          };
        }
        return {
          data:
            offset === "0"
              ? [
                  {
                    inbound_email_id: "recent",
                    from_address: "otp@provider.test",
                    subject: "Recent email",
                    body_text: "Your code is 123456",
                    links: [],
                    category: "otp",
                    created_at: "2026-08-01T00:00:00",
                  },
                ]
              : [],
          headers: {
            "x-has-more": "true",
            "x-next-cursor": String(Number(offset) + 200),
          },
        };
      },
    );
    try {
      mount(<ForwardingInbox addressId="fea_test" />);
      for (let i = 0; i < 10 && !screen.queryByText("Recent email"); i++) {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(100);
        });
      }
      expect(screen.getByText("Recent email")).toBeTruthy();
      expect(
        screen.getByRole("button", { name: "Continue searching" }),
      ).toBeTruthy();
      expect(offsets).toEqual(["0", "200", "400"]);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000);
      });
      expect(offsets).toEqual(["0", "200", "400", "0"]);
      expect(screen.getByText("Recent email")).toBeTruthy();
      fireEvent.click(
        screen.getByRole("button", { name: "Continue searching" }),
      );
      for (let i = 0; i < 10 && !screen.queryByText("Older after poll"); i++) {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(100);
        });
      }
      expect(screen.getByText("Older after poll")).toBeTruthy();
      expect(offsets[4]).toBe("600");
    } finally {
      cleanup();
      vi.useRealTimers();
    }
  });

  it("pauses polling for multi-window results and refreshes from the newest page", async () => {
    vi.useFakeTimers();
    const offsets: string[] = [];
    let refreshed = false;
    const email = (id: string, subject: string) => ({
      inbound_email_id: id,
      from_address: "otp@provider.test",
      subject,
      body_text: "Your code is 123456",
      links: [],
      category: "otp",
      created_at: "2026-08-01T00:00:00",
    });
    api.get.mockImplementation(
      async (_url: string, options: { params: { offset: string } }) => {
        const offset = options.params.offset;
        offsets.push(offset);
        if (offset === "200" && refreshed) {
          return {
            data: [email("fresh", "Refreshed message")],
            headers: { "x-has-more": "false" },
          };
        }
        if (offset === "600") {
          return {
            data: [email("older", "Older page")],
            headers: { "x-has-more": "false" },
          };
        }
        return {
          data:
            offset === "0"
              ? [email("recent", "Newest email")]
              : offset === "200"
                ? [email("later", "Later email")]
                : [],
          headers: {
            "x-has-more": "true",
            "x-next-cursor": String(Number(offset) + 200),
          },
        };
      },
    );
    try {
      mount(<ForwardingInbox addressId="fea_test" />);
      for (let i = 0; i < 10 && !screen.queryByText("Later email"); i++) {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(100);
        });
      }
      expect(screen.getByText("Later email")).toBeTruthy();
      expect(offsets).toEqual(["0", "200", "400"]);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(20_000);
      });
      expect(offsets).toEqual(["0", "200", "400"]);
      await act(async () => {
        focusManager.setFocused(false);
        focusManager.setFocused(true);
        onlineManager.setOnline(false);
        onlineManager.setOnline(true);
        await vi.advanceTimersByTimeAsync(100);
      });
      expect(offsets).toEqual(["0", "200", "400"]);

      fireEvent.click(
        screen.getByRole("button", { name: "Continue searching" }),
      );
      for (let i = 0; i < 10 && !screen.queryByText("Older page"); i++) {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(100);
        });
      }
      expect(screen.getByText("Page 2")).toBeTruthy();
      refreshed = true;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(100);
      });
      fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
      for (let i = 0; i < 10 && !screen.queryByText("Refreshed message"); i++) {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(100);
        });
      }
      expect(screen.getByText("Refreshed message")).toBeTruthy();
      expect(screen.getByText("Page 1")).toBeTruthy();
      expect(screen.queryByText("Older page")).toBeNull();
      expect(screen.queryByText("Later email")).toBeNull();
    } finally {
      cleanup();
      focusManager.setFocused(undefined);
      onlineManager.setOnline(true);
      vi.useRealTimers();
    }
  });

  it("continues searching when the first bounded scan finds no visible emails", async () => {
    api.get.mockImplementation(
      async (_url: string, options: { params: { offset: string } }) => {
        const offset = options.params.offset;
        return offset === "600"
          ? {
              data: [
                {
                  inbound_email_id: "older",
                  from_address: "otp@provider.test",
                  subject: "Older visible email",
                  body_text: "Your code is 123456",
                  links: [],
                  category: "otp",
                  created_at: "2026-08-01T00:00:00",
                },
              ],
              headers: { "x-has-more": "false" },
            }
          : {
              data: [],
              headers: {
                "x-has-more": "true",
                "x-next-cursor": String(Number(offset) + 200),
              },
            };
      },
    );
    mount(<ForwardingInbox addressId="fea_test" />);

    await screen.findByText(/No visible emails found yet/);
    expect(screen.queryByText(/No received emails/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Continue searching" }));
    await screen.findByText("Older visible email");
    expect(screen.getByText("Page 1")).toBeTruthy();
  });

  it("rechecks later windows on an explicit refresh of the newest page", async () => {
    let accessChanged = false;
    const email = (id: string, subject: string) => ({
      inbound_email_id: id,
      from_address: "otp@provider.test",
      subject,
      body_text: "Your code is 123456",
      links: [],
      category: "otp",
      created_at: "2026-08-01T00:00:00",
    });
    api.get.mockImplementation(
      async (_url: string, options: { params: { offset: string } }) =>
        options.params.offset === "0"
          ? {
              data: [email("recent", "Newest email")],
              headers: { "x-has-more": "true", "x-next-cursor": "200" },
            }
          : {
              data: accessChanged
                ? [email("new-access", "Newly accessible email")]
                : [email("old-access", "Previously accessible email")],
              headers: { "x-has-more": "false" },
            },
    );
    mount(<ForwardingInbox addressId="fea_test" />);

    await screen.findByText("Previously accessible email");
    accessChanged = true;
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await screen.findByText("Newly accessible email");
    expect(screen.queryByText("Previously accessible email")).toBeNull();
  });

  it("stops on a cycling inbox continuation", async () => {
    api.get.mockImplementation(
      async (_url: string, options: { params: { offset: string } }) => ({
        data: [],
        headers: {
          "x-has-more": "true",
          "x-next-cursor": options.params.offset === "0" ? "200" : "0",
        },
      }),
    );
    mount(<ForwardingInbox addressId="fea_test" />);

    expect(await screen.findByRole("alert")).toBeTruthy();
  });

  it("lets a Builder rotate a managed address without listing admin-only configs", async () => {
    let rotated = false;
    const managedAddress = {
      ...address,
      email_address: "email-test-abc123@email.skyvern.com",
    };
    const replacement = {
      ...managedAddress,
      forwarding_address_id: "fea_new",
      email_address: "email-test-new@email.example",
    };
    api.get.mockImplementation(async (url: string) => {
      if (url.endsWith("configs"))
        throw new Error("Builder cannot list configs");
      return { data: rotated ? [replacement] : [managedAddress] };
    });
    api.post.mockImplementation(async () => {
      rotated = true;
      return {
        data: {
          revoked: { ...managedAddress, status: "revoked" },
          replacement,
        },
      };
    });
    mount(<EmailForwardingSettings />);
    fireEvent.click(await screen.findByRole("button", { name: "Rotate" }));
    expect(api.post).not.toHaveBeenCalled();
    fireEvent.click(
      await screen.findByRole("button", { name: "Rotate address" }),
    );
    expect(
      await screen.findByText(/Update your mail forwarding rule/),
    ).toBeTruthy();
    expect(api.get.mock.calls.some(([url]) => url.endsWith("/configs"))).toBe(
      false,
    );
    expect(api.post).toHaveBeenCalledWith(
      "/email-forwarding/addresses/fea_test/rotate-managed",
      undefined,
    );
    expect(
      await within(screen.getByRole("table")).findByText(
        replacement.email_address,
      ),
    ).toBeTruthy();
  });

  it("tells users to update credentials after rotating an unbound dedicated address", async () => {
    const unboundAddress = { ...address, credential_id: null };
    const replacement = {
      ...unboundAddress,
      forwarding_address_id: "fea_new",
      email_address: "email-test-new@email.example",
    };
    api.get.mockResolvedValue({ data: [unboundAddress] });
    api.post.mockResolvedValue({ data: { replacement } });
    mount(<EmailForwardingSettings />);

    fireEvent.click(await screen.findByRole("button", { name: "Rotate" }));
    expect(
      await screen.findByText(
        /this dedicated address is not linked to a credential/i,
      ),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Rotate address" }));

    expect(
      await screen.findByText(
        /update the email identifier in any credential that used the old address/i,
      ),
    ).toBeTruthy();
  });

  it("offers no lifecycle mutations for a revoked address", async () => {
    api.get.mockResolvedValue({ data: [{ ...address, status: "revoked" }] });
    mount(<EmailForwardingSettings />);
    await screen.findByText(address.email_address);
    expect(
      screen.queryByRole("button", {
        name: /^(Rotate|Enable|Disable|Revoke)$/,
      }),
    ).toBeNull();
  });

  it("falls back to the standard route for webhook address rotation", async () => {
    const replacement = {
      ...address,
      email_address: "replacement@inbound.example",
    };
    const notManaged = Object.assign(new Error("Not a managed address"), {
      isAxiosError: true,
      response: {
        status: 409,
        data: {
          detail:
            "Not a managed address; rotate it through the standard rotate endpoint.",
        },
      },
    });
    api.get.mockResolvedValue({ data: [address] });
    api.post.mockImplementation(async (url: string) => {
      if (url.endsWith("rotate-managed")) throw notManaged;
      return { data: { replacement } };
    });
    mount(<EmailForwardingSettings />);

    fireEvent.click(await screen.findByRole("button", { name: "Rotate" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "Rotate address" }),
    );
    fireEvent.change(
      await screen.findByLabelText("Replacement forwarding address"),
      { target: { value: replacement.email_address } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Rotate address" }));

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith(
        "/email-forwarding/addresses/fea_test/rotate",
        { email_address: replacement.email_address },
      ),
    );
  });
});
