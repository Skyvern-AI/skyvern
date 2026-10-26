// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { StreamPresenter } from "./StreamPresenter";

const runtimeConfigMock = vi.hoisted(() => ({
  browserStreamingMode: "cdp" as "cdp" | "vnc",
  transportPending: false,
}));

const cdpStreamProps = vi.hoisted(
  () =>
    ({ last: null }) as {
      last: { enableUrlInput?: boolean } | null;
    },
);

vi.mock("@/hooks/useRuntimeConfig", () => ({
  useBrowserStreamingMode: () => ({
    browserStreamingMode: runtimeConfigMock.browserStreamingMode,
  }),
  useStreamTransport: () => ({
    streamTransport: runtimeConfigMock.transportPending
      ? undefined
      : runtimeConfigMock.browserStreamingMode,
  }),
}));

vi.mock("@/components/BrowserStream", () => ({
  BrowserStream: () => {
    return <div data-testid="vnc-stream" />;
  },
}));

vi.mock("@/routes/browserSessions/BrowserSessionStream", () => ({
  BrowserSessionStream: (props: { enableUrlInput?: boolean }) => {
    cdpStreamProps.last = props;
    return <div data-testid="cdp-stream" />;
  },
}));

describe("StreamPresenter", () => {
  afterEach(() => {
    cleanup();
    cdpStreamProps.last = null;
    runtimeConfigMock.browserStreamingMode = "cdp";
    runtimeConfigMock.transportPending = false;
  });

  it("forwards the URL input opt-in to the CDP stream", () => {
    // CDP streams the page viewport only, so the navigable bar is the pane's
    // sole way off about:blank; VNC carries the browser's own chrome (SKY-13705).
    render(<StreamPresenter browserSessionId="pbs_test" enableUrlInput />);
    expect(cdpStreamProps.last?.enableUrlInput).toBe(true);
  });

  it("leaves the URL bar read-only when the caller does not opt in", () => {
    render(<StreamPresenter browserSessionId="pbs_test" />);
    expect(cdpStreamProps.last?.enableUrlInput).toBe(false);
  });

  it("shows neither stream until the session's transport is known", () => {
    runtimeConfigMock.transportPending = true;
    // Global mode is cdp, but an unanswered session must not be painted as either: a session
    // that turns out to stream the other way would have shown a stream that cannot connect.
    render(<StreamPresenter browserSessionId="pbs_test" />);

    expect(screen.queryByTestId("cdp-stream")).toBeNull();
    expect(screen.queryByTestId("vnc-stream")).toBeNull();
  });

  it("shows the CDP stream in cdp mode", () => {
    render(<StreamPresenter browserSessionId="pbs_test" />);
    expect(screen.queryByTestId("cdp-stream")).not.toBeNull();
    expect(screen.queryByTestId("vnc-stream")).toBeNull();
  });

  it("shows the VNC stream on a vnc-transport session", () => {
    runtimeConfigMock.browserStreamingMode = "vnc";
    render(<StreamPresenter browserSessionId="pbs_test" />);
    expect(screen.queryByTestId("vnc-stream")).not.toBeNull();
    expect(screen.queryByTestId("cdp-stream")).toBeNull();
  });
});
