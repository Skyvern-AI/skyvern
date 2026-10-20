// @vitest-environment jsdom

import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

const { getMock, postMock, patchMock, deleteMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  postMock: vi.fn(),
  patchMock: vi.fn(),
  deleteMock: vi.fn(),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));
vi.mock("@/api/AxiosClient", () => ({
  getClient: () =>
    Promise.resolve({
      get: getMock,
      post: postMock,
      patch: patchMock,
      delete: deleteMock,
    }),
}));
vi.mock("@/components/ui/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

import { useGoogleOAuthCredentials } from "./useGoogleOAuthCredentials";
import { useMicrosoftOAuthCredentials } from "./useMicrosoftOAuthCredentials";
import { GoogleOAuthCredentialSelector } from "@/routes/workflows/components/GoogleOAuthCredentialSelector";
import { MicrosoftOAuthCredentialSelector } from "@/routes/workflows/components/MicrosoftOAuthCredentialSelector";
import { PageSlotsProvider, type PermissionsState } from "@/store/PageSlots";

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

function makeWrapper(
  permissions: PermissionsState = memberPermissions,
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  }),
) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        <PageSlotsProvider value={{ permissions }}>
          {children}
        </PageSlotsProvider>
      </QueryClientProvider>
    );
  };
}

afterEach(() => {
  cleanup();
  getMock.mockReset();
  postMock.mockReset();
  patchMock.mockReset();
  deleteMock.mockReset();
});

it.each([
  ["Google", useGoogleOAuthCredentials, "/google/oauth/credentials"],
  ["Microsoft", useMicrosoftOAuthCredentials, "/microsoft/oauth/credentials"],
])(
  "loads Member-safe %s credentials even when no list capability is returned",
  async (_provider, useHook, listPath) => {
    getMock.mockResolvedValue({
      data: { credentials: [{ id: "member-visible-credential" }] },
    });

    const { result } = renderHook(() => useHook(), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => expect(getMock).toHaveBeenCalledWith(listPath));
    await waitFor(() =>
      expect(result.current.credentials).toEqual([
        { id: "member-visible-credential" },
      ]),
    );
  },
);

it.each([
  ["Google", GoogleOAuthCredentialSelector, "/google/oauth/credentials"],
  [
    "Microsoft",
    MicrosoftOAuthCredentialSelector,
    "/microsoft/oauth/credentials",
  ],
])(
  "keeps the %s connect option disabled for Members",
  async (_provider, Selector, listPath) => {
    getMock.mockResolvedValue({ data: { credentials: [] } });

    render(
      <Selector
        nodeId="node_1"
        value=""
        onChange={vi.fn()}
        requiredScopes={[]}
      />,
      { wrapper: makeWrapper() },
    );

    await waitFor(() => expect(getMock).toHaveBeenCalledWith(listPath));
    fireEvent.click(screen.getByRole("combobox"));
    expect(
      screen
        .getByRole("option", {
          name: "Connect new account (admin required)",
        })
        .getAttribute("aria-disabled"),
    ).toBe("true");
  },
);

it.each([
  ["Google", useGoogleOAuthCredentials, "/google/oauth/credentials"],
  ["Microsoft", useMicrosoftOAuthCredentials, "/microsoft/oauth/credentials"],
])(
  "loads Member-safe %s credentials while permissions are unresolved",
  async (_provider, useHook, listPath) => {
    getMock.mockResolvedValue({ data: { credentials: [] } });

    renderHook(() => useHook(), {
      wrapper: makeWrapper({ status: "loading" }),
    });

    await waitFor(() => expect(getMock).toHaveBeenCalledWith(listPath));
  },
);

it.each([
  ["Google", useGoogleOAuthCredentials, "/google/oauth/credentials"],
  ["Microsoft", useMicrosoftOAuthCredentials, "/microsoft/oauth/credentials"],
])(
  "blocks %s OAuth mutations for Members but retains credential listing",
  async (_provider, useHook, listPath) => {
    getMock.mockResolvedValue({ data: { credentials: [] } });

    const { result } = renderHook(() => useHook(), { wrapper: makeWrapper() });

    await waitFor(() => expect(getMock).toHaveBeenCalledWith(listPath));
    await expect(result.current.startAuthorize({} as never)).rejects.toThrow(
      "Organization admin access is required",
    );
    await expect(
      result.current.submitOAuthCallbackAsync({ code: "code", state: "state" }),
    ).rejects.toThrow("Organization admin access is required");
    await expect(
      result.current.renameCredentialAsync({
        credentialId: "credential",
        credentialName: "name",
      }),
    ).rejects.toThrow("Organization admin access is required");
    await expect(
      result.current.deleteCredentialAsync("credential"),
    ).rejects.toThrow("Organization admin access is required");

    expect(postMock).not.toHaveBeenCalled();
    expect(patchMock).not.toHaveBeenCalled();
    expect(deleteMock).not.toHaveBeenCalled();
  },
);
