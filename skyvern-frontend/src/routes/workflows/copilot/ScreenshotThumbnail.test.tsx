// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";

const getMock = vi.hoisted(() => vi.fn());

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn(async () => ({ get: getMock })),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

import { ScreenshotThumbnail } from "./ScreenshotThumbnail";

type FakeObserver = {
  callback: IntersectionObserverCallback;
  elements: Element[];
};

let observers: FakeObserver[] = [];

beforeEach(() => {
  observers = [];
  getMock.mockReset();
  getMock.mockImplementation(async (path: string) => {
    const artifactId = path.split("/").pop();
    return {
      data: {
        artifact_id: artifactId,
        artifact_type: "screenshot_action",
        uri: `s3://bucket/${artifactId}.png`,
        signed_url: `https://api.skyvern.com/v1/artifacts/${artifactId}/content`,
      },
    };
  });

  class FakeIntersectionObserver {
    private entry: FakeObserver;
    constructor(callback: IntersectionObserverCallback) {
      this.entry = { callback, elements: [] };
      observers.push(this.entry);
    }
    observe = (element: Element) => {
      this.entry.elements.push(element);
    };
    disconnect = () => {};
    unobserve = () => {};
    takeRecords = () => [];
    root = null;
    rootMargin = "";
    thresholds = [];
  }
  globalThis.IntersectionObserver =
    FakeIntersectionObserver as unknown as typeof IntersectionObserver;
});

function renderThumbnails(artifactIds: Array<string>) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      {artifactIds.map((artifactId) => (
        <ScreenshotThumbnail key={artifactId} artifactId={artifactId} />
      ))}
    </QueryClientProvider>,
  );
}

function reveal(element: Element) {
  const observer = observers.find((entry) => entry.elements.includes(element));
  if (!observer) throw new Error("element is not observed");
  act(() => {
    observer.callback(
      [{ isIntersecting: true, target: element } as IntersectionObserverEntry],
      {} as IntersectionObserver,
    );
  });
}

function requestedPaths() {
  return getMock.mock.calls.map((call) => call[0] as string);
}

describe("ScreenshotThumbnail artifact fan-out (SKY-19468)", () => {
  test("requests nothing while every thumbnail is away from the viewport", async () => {
    const ids = Array.from({ length: 40 }, (_, index) => `a_${index}`);
    renderThumbnails(ids);

    await waitFor(() => expect(observers.length).toBe(ids.length));
    expect(requestedPaths()).toEqual([]);
  });

  test("requests only the artifact of the thumbnail that comes into view", async () => {
    const ids = Array.from({ length: 40 }, (_, index) => `a_${index}`);
    renderThumbnails(ids);
    await waitFor(() => expect(observers.length).toBe(ids.length));

    const buttons = screen.getAllByRole("button", { name: "View screenshot" });
    reveal(buttons[7]!);

    await waitFor(() => expect(requestedPaths()).toEqual(["/artifacts/a_7"]));
    await waitFor(() =>
      expect(buttons[7]!.querySelector("img")?.getAttribute("src")).toBe(
        "https://api.skyvern.com/v1/artifacts/a_7/content",
      ),
    );
    expect(requestedPaths()).toEqual(["/artifacts/a_7"]);
  });

  test("requests immediately when IntersectionObserver is unavailable", async () => {
    // @ts-expect-error -- exercising the browser that cannot gate on visibility.
    delete globalThis.IntersectionObserver;
    renderThumbnails(["a_only"]);

    await waitFor(() =>
      expect(requestedPaths()).toEqual(["/artifacts/a_only"]),
    );
  });
});
