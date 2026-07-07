// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { StreamPresenter } from "./StreamPresenter";

const runtimeConfigMock = vi.hoisted(() => ({
  browserStreamingMode: "cdp" as "cdp" | "vnc",
}));

const browserStreamProps = vi.hoisted(
  () =>
    ({ last: null }) as { last: { resetRecordingOnUnmount?: boolean } | null },
);

vi.mock("@/hooks/useRuntimeConfig", () => ({
  useBrowserStreamingMode: () => ({
    browserStreamingMode: runtimeConfigMock.browserStreamingMode,
  }),
}));

vi.mock("@/components/BrowserStream", () => ({
  BrowserStream: (props: { resetRecordingOnUnmount?: boolean }) => {
    browserStreamProps.last = props;
    return <div data-testid="vnc-stream" />;
  },
}));

vi.mock("@/routes/browserSessions/BrowserSessionStream", () => ({
  BrowserSessionStream: () => <div data-testid="cdp-stream" />,
}));

describe("StreamPresenter transport-swap recording", () => {
  afterEach(() => {
    cleanup();
    browserStreamProps.last = null;
    runtimeConfigMock.browserStreamingMode = "cdp";
  });

  it("shows the CDP stream when not recording in cdp mode", () => {
    render(<StreamPresenter browserSessionId="pbs_test" />);
    expect(screen.queryByTestId("cdp-stream")).not.toBeNull();
    expect(screen.queryByTestId("vnc-stream")).toBeNull();
  });

  it("swaps to the VNC stream and opts it out of the unmount reset when recording starts", () => {
    const { rerender } = render(
      <StreamPresenter browserSessionId="pbs_test" isRecording={false} />,
    );
    expect(screen.queryByTestId("cdp-stream")).not.toBeNull();

    // Recording forces VNC: the CDP stream unmounts, the fresh VNC stream mounts.
    rerender(<StreamPresenter browserSessionId="pbs_test" isRecording />);
    expect(screen.queryByTestId("vnc-stream")).not.toBeNull();
    expect(screen.queryByTestId("cdp-stream")).toBeNull();
    expect(browserStreamProps.last?.resetRecordingOnUnmount).toBe(false);
  });
});
