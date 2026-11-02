// @vitest-environment jsdom
import type { ReactNode } from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { posthogMock, routerMount } = vi.hoisted(() => ({
  posthogMock: { init: vi.fn() },
  routerMount: vi.fn(),
}));

vi.mock("posthog-js", () => ({ default: posthogMock }));
vi.mock("posthog-js/react", () => ({
  PostHogProvider: ({ children }: { children: ReactNode }) => children,
}));
vi.mock("react-router-dom", async () => {
  const React = await import("react");
  const { useForm } = await import("react-hook-form");

  return {
    RouterProvider: () => {
      React.useEffect(() => {
        routerMount();
      }, []);
      const { register } = useForm<{ taskName: string }>({
        defaultValues: { taskName: "" },
      });
      return React.createElement(
        "div",
        { "data-testid": "feature-flag-consumers" },
        React.createElement("input", {
          "aria-label": "Task name",
          ...register("taskName"),
        }),
      );
    },
  };
});
vi.mock("./router", () => ({ router: {} }));
vi.mock("./api/QueryClient", () => ({ queryClient: {} }));
vi.mock("@tanstack/react-query", () => ({
  QueryClientProvider: ({ children }: { children: ReactNode }) => children,
}));
vi.mock("@/components/ThemeProvider", () => ({
  ThemeProvider: ({ children }: { children: ReactNode }) => children,
}));
vi.mock("@/providers/RuntimeFeatureFlagProvider", () => ({
  RuntimeFeatureFlagProvider: ({ children }: { children: ReactNode }) =>
    children,
}));
vi.mock("@/providers/OnPremOidcProvider", () => ({
  OnPremOidcProvider: ({ children }: { children: ReactNode }) => children,
}));

beforeEach(() => {
  vi.resetModules();
  posthogMock.init.mockReset();
  routerMount.mockReset();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("App PostHog initialization", () => {
  it("renders while the runtime config request remains pending", async () => {
    vi.useFakeTimers();
    let requestSignal!: AbortSignal;
    const fetchMock = vi.fn(
      (_input: RequestInfo | URL, options?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          const signal = options?.signal;
          if (!signal) {
            throw new Error("Expected an abort signal");
          }
          requestSignal = signal as AbortSignal;
          requestSignal.addEventListener(
            "abort",
            () => reject(new DOMException("Aborted", "AbortError")),
            { once: true },
          );
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { default: App } = await import("./App");
    render(<App />);

    expect(screen.getByTestId("feature-flag-consumers")).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(posthogMock.init).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(requestSignal.aborted).toBe(true);
    expect(screen.getByTestId("feature-flag-consumers")).toBeTruthy();
    expect(routerMount).toHaveBeenCalledTimes(1);
  });

  it("preserves form state and keeps the route mounted after PostHog initializes", async () => {
    let resolveConfig!: (response: Response) => void;
    const fetchMock = vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          resolveConfig = resolve;
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { default: App } = await import("./App");
    render(<App />);
    expect(routerMount).toHaveBeenCalledTimes(1);
    fireEvent.change(screen.getByLabelText("Task name"), {
      target: { value: "unsaved task draft" },
    });

    await act(async () => {
      resolveConfig({
        ok: true,
        json: async () => ({
          api_key: "configured-public-key",
          api_host: "https://posthog.example",
        }),
      } as Response);
    });

    expect(posthogMock.init).toHaveBeenCalledOnce();
    expect(routerMount).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText<HTMLInputElement>("Task name").value).toBe(
      "unsaved task draft",
    );
  });
});
