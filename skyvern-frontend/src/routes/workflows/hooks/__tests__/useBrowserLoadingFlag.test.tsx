// @vitest-environment jsdom

import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { useSettingsStore } from "@/store/SettingsStore";

import { useBrowserLoadingFlag } from "../useBrowserLoadingFlag";

const initialSettings = useSettingsStore.getState();

function Harness({
  shouldFetchDebugSession,
  readyBrowserSessionId,
}: {
  shouldFetchDebugSession: boolean;
  readyBrowserSessionId: string | null;
}) {
  useBrowserLoadingFlag(shouldFetchDebugSession, readyBrowserSessionId);
  return null;
}

describe("useBrowserLoadingFlag — closes the pre-mount visibility gap (SKY-9777)", () => {
  beforeEach(() => {
    useSettingsStore.setState(initialSettings, true);
  });

  afterEach(() => {
    cleanup();
    useSettingsStore.setState(initialSettings, true);
  });

  it("flips isLoadingABrowser to true as soon as session fetch begins, BEFORE BrowserStream mounts (regression: this is the window Winton screenshotted on the original PR)", () => {
    // Editor mounts → useMountEffect → setShouldFetchDebugSession(true).
    // useDebugSessionQuery is now polling. activeDebugSession is still null
    // and therefore BrowserStream is NOT YET MOUNTED with a session id.
    render(
      <Harness shouldFetchDebugSession={true} readyBrowserSessionId={null} />,
    );

    expect(useSettingsStore.getState().isLoadingABrowser).toBe(true);
    expect(useSettingsStore.getState().isUsingABrowser).toBe(false);
  });

  it("stays true through the BrowserStream connect window (session id known, VNC not yet ready)", () => {
    // useDebugSessionQuery returned. activeDebugSession now has a
    // browser_session_id. BrowserStream is mounted but VNC handshake hasn't
    // completed → readyBrowserSessionId is still null.
    render(
      <Harness shouldFetchDebugSession={true} readyBrowserSessionId={null} />,
    );

    expect(useSettingsStore.getState().isLoadingABrowser).toBe(true);
  });

  it("flips isLoadingABrowser back to false once the browser is ready", () => {
    const { rerender } = render(
      <Harness shouldFetchDebugSession={true} readyBrowserSessionId={null} />,
    );

    expect(useSettingsStore.getState().isLoadingABrowser).toBe(true);

    rerender(
      <Harness
        shouldFetchDebugSession={true}
        readyBrowserSessionId="bs_test_ready"
      />,
    );

    expect(useSettingsStore.getState().isLoadingABrowser).toBe(false);
  });

  it("stays false when the editor is not fetching a session (e.g. browser panel never opened)", () => {
    render(
      <Harness shouldFetchDebugSession={false} readyBrowserSessionId={null} />,
    );

    expect(useSettingsStore.getState().isLoadingABrowser).toBe(false);
  });

  it("clears the flag on unmount so navigating away doesn't strand the menu in loading state", () => {
    const { unmount } = render(
      <Harness shouldFetchDebugSession={true} readyBrowserSessionId={null} />,
    );

    expect(useSettingsStore.getState().isLoadingABrowser).toBe(true);

    unmount();

    expect(useSettingsStore.getState().isLoadingABrowser).toBe(false);
  });
});
