import { describe, expect, it } from "vitest";

import {
  resolveBrowserPanelMode,
  resolveCdpRecordingExfiltrate,
  updateBrowserPanelLatch,
} from "./browserPanelMode";

describe("resolveBrowserPanelMode", () => {
  it("downgrades a flag-off CDP recording to VNC", () => {
    expect(
      resolveBrowserPanelMode({
        streamTransport: "cdp",
        isRecording: true,
        cdpRecordingEnabled: false,
      }),
    ).toEqual({ showCdp: false, preferVnc: true });
  });

  it("keeps a flag-on CDP recording on CDP", () => {
    expect(
      resolveBrowserPanelMode({
        streamTransport: "cdp",
        isRecording: true,
        cdpRecordingEnabled: true,
      }),
    ).toEqual({ showCdp: true, preferVnc: false });
  });

  it("prefers VNC while the transport is pending", () => {
    expect(
      resolveBrowserPanelMode({
        streamTransport: undefined,
        isRecording: false,
        cdpRecordingEnabled: true,
      }),
    ).toEqual({ showCdp: false, preferVnc: true });
  });
});

describe("updateBrowserPanelLatch", () => {
  it("keeps a pending transport from switching to CDP mid-recording", () => {
    const latch = {
      streamTransport: undefined,
      cdpRecordingEnabled: true,
    } as const;

    const duringRecording = updateBrowserPanelLatch(
      latch,
      {
        streamTransport: "cdp",
        cdpRecordingEnabled: true,
      },
      true,
    );
    expect(
      resolveBrowserPanelMode({
        ...duringRecording,
        isRecording: true,
      }),
    ).toEqual({ showCdp: false, preferVnc: true });

    const afterRecording = updateBrowserPanelLatch(
      duringRecording,
      {
        streamTransport: "cdp",
        cdpRecordingEnabled: true,
      },
      false,
    );
    expect(
      resolveBrowserPanelMode({
        ...afterRecording,
        isRecording: false,
      }),
    ).toEqual({ showCdp: true, preferVnc: false });
  });
});

describe("resolveCdpRecordingExfiltrate", () => {
  it("leaves CDP recording messages disabled while the flag is off", () => {
    expect(
      resolveCdpRecordingExfiltrate({
        cdpRecordingEnabled: false,
        isRecording: true,
        finishRequested: false,
      }),
    ).toBeUndefined();
  });
});
