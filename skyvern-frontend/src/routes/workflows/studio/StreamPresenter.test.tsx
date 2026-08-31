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

  it("opts the VNC stream out of the unmount reset while recording", () => {
    runtimeConfigMock.browserStreamingMode = "vnc";
    render(<StreamPresenter browserSessionId="pbs_test" isRecording />);
    expect(screen.queryByTestId("vnc-stream")).not.toBeNull();
    expect(browserStreamProps.last?.resetRecordingOnUnmount).toBe(false);
  });
});

describe("StreamPresenter recording on a cdp-transport session", () => {
  afterEach(() => {
    cleanup();
    browserStreamProps.last = null;
    cdpStreamProps.last = null;
    runtimeConfigMock.browserStreamingMode = "cdp";
    runtimeConfigMock.transportPending = false;
    useRecordingStore.getState().reset();
  });

  it("keeps the CDP stream and drives exfiltration through it when recording starts", () => {
    // A cdp-transport session is one with no relayable RFB endpoint (a vendor
    // browser). Swapping it to VNC on record tore down the only working stream
    // and left the viewer dead while the API took a reconnect storm.
    useRecordingStore
      .getState()
      .setIsRecording(true, { workflowPermanentId: "wpid_test" });

    render(<StreamPresenter browserSessionId="pbs_test" isRecording />);

    expect(screen.queryByTestId("cdp-stream")).not.toBeNull();
    expect(screen.queryByTestId("vnc-stream")).toBeNull();
    expect(cdpStreamProps.last?.exfiltrate).toBe(true);
    expect(cdpStreamProps.last?.workflowPermanentId).toBe("wpid_test");
  });

  it("keeps the recording message channel closed on the idle live view", () => {
    // BrowserSessionStream opens its recording WebSocket whenever exfiltrate is
    // defined, so the non-recording view must pass undefined, not false.
    render(<StreamPresenter browserSessionId="pbs_test" isRecording={false} />);
    expect(cdpStreamProps.last?.exfiltrate).toBeUndefined();
  });

  it("labels the recording with the transport it actually rode", () => {
    // useProcessRecordingMutation reports store.recordingTransport to the backend;
    // with nothing setting it, every CDP recording was mislabelled as vnc.
    render(<StreamPresenter browserSessionId="pbs_test" />);
    expect(useRecordingStore.getState().recordingTransport).toBe("cdp");
  });

  it("still records over VNC on a vnc-transport session", () => {
    runtimeConfigMock.browserStreamingMode = "vnc";
    render(<StreamPresenter browserSessionId="pbs_test" isRecording />);
    expect(screen.queryByTestId("vnc-stream")).not.toBeNull();
    expect(screen.queryByTestId("cdp-stream")).toBeNull();
  });
});
