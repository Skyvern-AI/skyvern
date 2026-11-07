import { describe, expect, it } from "vitest";

import {
  canViewBrowserSessionLive,
  getBrowserSessionTabFromPathname,
  getBrowserSessionTabOptions,
  getSessionControlsState,
} from "./BrowserSession.utils";

describe("getBrowserSessionTabFromPathname", () => {
  it.each([
    ["/browser-session/session-1/timeline", "timeline"],
    ["/browser-session/session-1/downloads", "downloads"],
  ] as const)("maps %s to %s", (pathname, tab) => {
    expect(getBrowserSessionTabFromPathname(pathname)).toBe(tab);
  });
});

describe("getBrowserSessionTabOptions", () => {
  it("hides the live-view tab when the connect address is withheld", () => {
    expect(
      getBrowserSessionTabOptions(false).map(({ to }) => to),
    ).not.toContain("stream");
  });

  it("keeps the live-view tab when a VNC session is available", () => {
    expect(getBrowserSessionTabOptions(true).map(({ to }) => to)).toContain(
      "stream",
    );
  });
});

describe("canViewBrowserSessionLive", () => {
  it("hides live-view affordances when the address was redacted", () => {
    const canViewLive = canViewBrowserSessionLive({
      browser_address: null,
      browser_address_redacted: true,
      source: "managed",
      status: "running",
      vnc_streaming_supported: false,
    });

    expect(canViewLive).toBe(false);
    expect(
      getBrowserSessionTabOptions(canViewLive).map(({ to }) => to),
    ).not.toContain("stream");
  });

  it("keeps external browser diagnostics available without an address", () => {
    expect(
      canViewBrowserSessionLive({
        browser_address: null,
        browser_address_redacted: false,
        source: "external",
        status: "running",
        vnc_streaming_supported: false,
      }),
    ).toBe(true);
  });

  it("keeps a starting session's live-view state until an address is available", () => {
    expect(
      canViewBrowserSessionLive({
        browser_address: null,
        browser_address_redacted: false,
        source: "managed",
        status: "created",
        vnc_streaming_supported: false,
      }),
    ).toBe(true);
  });
});

describe("getSessionControlsState", () => {
  const sessionId = "pbs_1";

  it("offers Save Profile on a running session with no save in flight", () => {
    expect(
      getSessionControlsState({
        browserSessionId: sessionId,
        status: "running",
        savingProfileSessionId: undefined,
      }),
    ).toEqual({
      showControls: true,
      isSavingProfile: false,
      showCloseSession: true,
    });
  });

  it("shows the saving state while this session's profile is being created", () => {
    expect(
      getSessionControlsState({
        browserSessionId: sessionId,
        status: "running",
        savingProfileSessionId: sessionId,
      }),
    ).toEqual({
      showControls: true,
      isSavingProfile: true,
      showCloseSession: true,
    });
  });

  it("ignores a save in flight for a different session", () => {
    expect(
      getSessionControlsState({
        browserSessionId: sessionId,
        status: "running",
        savingProfileSessionId: "pbs_2",
      }),
    ).toEqual({
      showControls: true,
      isSavingProfile: false,
      showCloseSession: true,
    });
  });

  it("keeps the saving state after the save closes the session", () => {
    expect(
      getSessionControlsState({
        browserSessionId: sessionId,
        status: "completed",
        savingProfileSessionId: sessionId,
      }),
    ).toEqual({
      showControls: true,
      isSavingProfile: true,
      showCloseSession: false,
    });
  });

  it("hides the controls once the save finishes on a closed session", () => {
    expect(
      getSessionControlsState({
        browserSessionId: sessionId,
        status: "completed",
        savingProfileSessionId: undefined,
      }),
    ).toEqual({
      showControls: false,
      isSavingProfile: false,
      showCloseSession: false,
    });
  });

  it("stays hidden when there is no session id to match against", () => {
    expect(
      getSessionControlsState({
        browserSessionId: undefined,
        status: undefined,
        savingProfileSessionId: undefined,
      }),
    ).toEqual({
      showControls: false,
      isSavingProfile: false,
      showCloseSession: false,
    });
  });
});
