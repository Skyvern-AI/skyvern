import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { GoogleOAuthCallback } from "./GoogleOAuthCallback";

const {
  broadcast,
  clearStored,
  closePopup,
  storedIntegrationId,
  submitCallback,
  toast,
} = vi.hoisted(() => ({
  broadcast: vi.fn(),
  clearStored: vi.fn(),
  closePopup: vi.fn(() => true),
  storedIntegrationId: { current: "google_sheets" as string | null },
  submitCallback: vi.fn(async () => ({ credential: {} })),
  toast: vi.fn(),
}));

vi.mock("@/hooks/useGoogleOAuthCredentials", () => ({
  broadcastGoogleOAuthCredentialsChanged: broadcast,
  useGoogleOAuthCredentials: () => ({
    submitOAuthCallbackAsync: submitCallback,
  }),
}));
vi.mock("@/components/ui/use-toast", () => ({
  useToast: () => ({ toast }),
}));
vi.mock("./googleOAuth", () => ({
  clearStoredGoogleOAuthIntegrationIdForState: clearStored,
  getStoredGoogleOAuthIntegrationIdForState: () => storedIntegrationId.current,
}));
vi.mock("./googleOAuthPopup", () => ({
  closeGoogleOAuthPopupIfMarked: closePopup,
}));

describe("GoogleOAuthCallback", () => {
  beforeEach(() => {
    broadcast.mockReset();
    clearStored.mockReset();
    closePopup.mockReset().mockReturnValue(true);
    storedIntegrationId.current = "google_sheets";
    submitCallback.mockReset().mockResolvedValue({ credential: {} });
    toast.mockReset();
  });

  it("closes and broadcasts after a stable host bounces back to this origin", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter
          initialEntries={[
            "/integrations/google/callback?credential_id=goac_1&success=1&state=state_1",
          ]}
        >
          <Routes>
            <Route
              path="/integrations/google/callback"
              element={<GoogleOAuthCallback />}
            />
            <Route path="/integrations" element={<div>Integrations</div>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await waitFor(() => expect(closePopup).toHaveBeenCalledTimes(1));
    expect(submitCallback).not.toHaveBeenCalled();
    expect(clearStored).toHaveBeenCalledWith("state_1");
    expect(broadcast).toHaveBeenCalledTimes(1);
  });

  it("does not announce an unrecognized bounced OAuth state", async () => {
    storedIntegrationId.current = null;
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const { findByText } = render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter
          initialEntries={[
            "/integrations/google/callback?credential_id=goac_1&success=1&state=forged",
          ]}
        >
          <Routes>
            <Route
              path="/integrations/google/callback"
              element={<GoogleOAuthCallback />}
            />
            <Route path="/integrations" element={<div>Integrations</div>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await findByText("Integrations")).not.toBeNull();
    expect(closePopup).not.toHaveBeenCalled();
    expect(broadcast).not.toHaveBeenCalled();
    expect(clearStored).not.toHaveBeenCalled();
  });

  it("closes a reconnect popup after exchanging the authorization code", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const { queryByText } = render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter
          initialEntries={[
            "/integrations/google/callback?code=code_1&state=state_1",
          ]}
        >
          <Routes>
            <Route
              path="/integrations/google/callback"
              element={<GoogleOAuthCallback />}
            />
            <Route path="/integrations" element={<div>Integrations</div>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await waitFor(() => expect(closePopup).toHaveBeenCalledTimes(1));
    expect(submitCallback).toHaveBeenCalledWith({
      code: "code_1",
      state: "state_1",
    });
    expect(queryByText("Integrations")).toBeNull();
  });
});
