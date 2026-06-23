import { AxiosError } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";

const getClientMock = vi.fn();

vi.mock("@/api/AxiosClient", () => ({
  getClient: (...args: unknown[]) => getClientMock(...args),
}));

function axiosErrorWithStatus(status: number): AxiosError {
  return new AxiosError(
    `Request failed with status code ${status}`,
    "ERR_BAD_REQUEST",
    undefined,
    undefined,
    // Minimal response shape: fetchDiagnostics only inspects response.status.
    { status, statusText: "", data: {}, headers: {}, config: {} as never },
  );
}

function mockDiagnosticsRejection(error: unknown) {
  getClientMock.mockResolvedValue({
    get: vi.fn().mockRejectedValue(error),
  });
}

describe("fetchDiagnostics", () => {
  afterEach(() => {
    vi.resetModules();
    getClientMock.mockReset();
  });

  it("fails open (status ok) when the diagnostics endpoint is missing (404)", async () => {
    const { fetchDiagnostics } = await import("./useAuthDiagnostics");
    mockDiagnosticsRejection(axiosErrorWithStatus(404));

    await expect(fetchDiagnostics()).resolves.toEqual({ status: "ok" });
  });

  // SKY-11308: a version-skewed / proxied backend can answer the local-only
  // diagnostics endpoint with a 401 (or 403). The endpoint can't help us in
  // that deployment, but the backend is clearly reachable, so we must NOT show
  // the misleading "could not reach the diagnostics endpoint / backend not
  // running" banner. Fail open like the 404 case instead.
  it("fails open (status ok) when diagnostics is rejected with 401", async () => {
    const { fetchDiagnostics } = await import("./useAuthDiagnostics");
    mockDiagnosticsRejection(axiosErrorWithStatus(401));

    await expect(fetchDiagnostics()).resolves.toEqual({ status: "ok" });
  });

  it("fails open (status ok) when diagnostics is rejected with 403", async () => {
    const { fetchDiagnostics } = await import("./useAuthDiagnostics");
    mockDiagnosticsRejection(axiosErrorWithStatus(403));

    await expect(fetchDiagnostics()).resolves.toEqual({ status: "ok" });
  });

  it("propagates genuine unreachability (no HTTP response) so the banner can warn", async () => {
    const { fetchDiagnostics } = await import("./useAuthDiagnostics");
    // Network error: no response attached -> backend truly unreachable.
    mockDiagnosticsRejection(new AxiosError("Network Error", "ERR_NETWORK"));

    await expect(fetchDiagnostics()).rejects.toThrow("Network Error");
  });

  it("propagates unexpected server errors (5xx) so real diagnostics bugs surface", async () => {
    const { fetchDiagnostics } = await import("./useAuthDiagnostics");
    mockDiagnosticsRejection(axiosErrorWithStatus(500));

    await expect(fetchDiagnostics()).rejects.toThrow(
      "Request failed with status code 500",
    );
  });
});
