// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useRecordingStore } from "@/store/useRecordingStore";

import { StreamPresenter } from "./StreamPresenter";

const runtimeConfigMock = vi.hoisted(() => ({
  browserStreamingMode: "cdp" as "cdp" | "vnc",
  transportPending: false,
}));

const browserStreamProps = vi.hoisted(
  () =>
    ({ last: null }) as { last: { resetRecordingOnUnmount?: boolean } | null },
);

const cdpStreamProps = vi.hoisted(
  () =>
    ({ last: null }) as {
      last: {
        enableUrlInput?: boolean;
        exfiltrate?: boolean;
        workflowPermanentId?: string | null;
      } | null;
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
  BrowserStream: (props: { resetRecordingOnUnmount?: boolean }) => {
    browserStreamProps.last = props;
    return <div data-testid="vnc-stream" />;
  },
}));

vi.mock("@/routes/browserSessions/BrowserSessionStream", () => ({
  BrowserSessionStream: (props: {
    enableUrlInput?: boolean;
    exfiltrate?: boolean;
    workflowPermanentId?: string | null;
  }) => {
    cdpStreamProps.last = props;
    return <div data-testid="cdp-stream" />;
  },
}));

describe("StreamPresenter transport-swap recording", () => {
  afterEach(() => {
    cleanup();
    browserStreamProps.last = null;
    cdpStreamProps.last = null;
    runtimeConfigMock.browserStreamingMode = "cdp";
    runtimeConfigMock.transportPending = false;
    useRecordingStore.getState().reset();
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

  it("shows the CDP stream when not recording in cdp mode", () => {
    render(<StreamPresenter browserSessionId="pbs_test" />);
    expect(screen.queryByTestId("cdp-stream")).not.toBeNull();
    expect(screen.queryByTestId("vnc-stream")).toBeNull();
  });

  it("keeps the recording message channel closed on the non-recording CDP live view", () => {
    // BrowserSessionStream opens its recording WebSocket whenever exfiltrate is
    // defined, so the idle live view must pass undefined, not false.
    render(<StreamPresenter browserSessionId="pbs_test" isRecording={false} />);
    expect(cdpStreamProps.last?.exfiltrate).toBeUndefined();
  });

  it("keeps the CDP stream and drives exfiltration through it when recording starts on cdp transport", () => {
    // A CDP-transport session has no reachable VNC endpoint: swapping to VNC on
    // record killed the live view and hammered the API with reconnects.
    useRecordingStore
      .getState()
      .setIsRecording(true, { workflowPermanentId: "wpid_test" });
    const { rerender } = render(
      <StreamPresenter browserSessionId="pbs_test" isRecording={false} />,
    );
    expect(screen.queryByTestId("recording-pill")).toBeNull();

    rerender(<StreamPresenter browserSessionId="pbs_test" isRecording />);
    expect(screen.queryByTestId("cdp-stream")).not.toBeNull();
    expect(screen.queryByTestId("vnc-stream")).toBeNull();
    expect(cdpStreamProps.last?.exfiltrate).toBe(true);
    expect(cdpStreamProps.last?.workflowPermanentId).toBe("wpid_test");
    expect(screen.queryByTestId("recording-pill")).not.toBeNull();
  });

  it("hides the CDP recording pill when the caller shows its own indicator", () => {
    useRecordingStore
      .getState()
      .setIsRecording(true, { workflowPermanentId: "wpid_test" });
    render(
      <StreamPresenter
        browserSessionId="pbs_test"
        isRecording
        hideRecordingIndicator
      />,
    );
    expect(screen.queryByTestId("recording-pill")).toBeNull();
  });

  it("records over the VNC stream, opted out of the unmount reset, on vnc transport", () => {
    runtimeConfigMock.browserStreamingMode = "vnc";
    render(<StreamPresenter browserSessionId="pbs_test" isRecording />);
    expect(screen.queryByTestId("vnc-stream")).not.toBeNull();
    expect(screen.queryByTestId("cdp-stream")).toBeNull();
    expect(browserStreamProps.last?.resetRecordingOnUnmount).toBe(false);
  });
});
