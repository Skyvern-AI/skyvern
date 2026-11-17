import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import { createElement, useEffect, useMemo, useRef, useState } from "react";
import { afterEach, describe, expect, it } from "vitest";

import {
  forgetDiscoverEmberPrompt,
  readDiscoverEmberPrompt,
  rememberDiscoverEmberPrompt,
  shouldOpenEmberPaneForHandoff,
  useDiscoverEmberPromptRecovery,
  withoutDiscoverViaParam,
} from "./discoverEmberHandoff";

afterEach(() => {
  cleanup();
  sessionStorage.clear();
});

function InitialMessageHarness({ sentMessages }: { sentMessages: string[] }) {
  const [routeMessage, setRouteMessage] = useState<string | null>(
    "Discover seed",
  );
  const { storedInitialEmberMessage, clearStoredInitialEmberMessage } =
    useDiscoverEmberPromptRecovery({
      shouldRead: true,
      workflowPermanentId: "wpid_1",
    });
  const initialMessage = useMemo(
    () => routeMessage ?? storedInitialEmberMessage,
    [routeMessage, storedInitialEmberMessage],
  );
  const hasAutoSentRef = useRef(false);

  useEffect(() => {
    hasAutoSentRef.current = false;
  }, [initialMessage]);

  useEffect(() => {
    if (!initialMessage || hasAutoSentRef.current) {
      return;
    }
    hasAutoSentRef.current = true;
    sentMessages.push(initialMessage);
    clearStoredInitialEmberMessage();
    setRouteMessage(null);
  }, [clearStoredInitialEmberMessage, initialMessage, sentMessages]);

  return createElement(
    "button",
    { onClick: () => setRouteMessage("Fix seed"), type: "button" },
    "send fix seed",
  );
}

function RecoveryHarness({
  shouldRead = true,
  workflowPermanentId,
}: {
  shouldRead?: boolean;
  workflowPermanentId: string | undefined;
}) {
  const { storedInitialEmberMessage, clearStoredInitialEmberMessage } =
    useDiscoverEmberPromptRecovery({
      shouldRead,
      workflowPermanentId,
    });

  return createElement(
    "div",
    null,
    createElement(
      "span",
      { "data-testid": "stored-prompt" },
      storedInitialEmberMessage ?? "",
    ),
    createElement(
      "button",
      { onClick: clearStoredInitialEmberMessage, type: "button" },
      "clear stored prompt",
    ),
  );
}

describe("discoverEmberHandoff", () => {
  it("stores and clears the prompt for one workflow", () => {
    rememberDiscoverEmberPrompt("wpid_1", "Build the workflow");

    expect(readDiscoverEmberPrompt("wpid_1")).toBe("Build the workflow");
    expect(readDiscoverEmberPrompt("wpid_1")).toBe("Build the workflow");

    forgetDiscoverEmberPrompt("wpid_1");
    expect(readDiscoverEmberPrompt("wpid_1")).toBeNull();
  });

  it("keeps prompts scoped by workflow id", () => {
    rememberDiscoverEmberPrompt("wpid_1", "First prompt");
    rememberDiscoverEmberPrompt("wpid_2", "Second prompt");

    expect(readDiscoverEmberPrompt("wpid_2")).toBe("Second prompt");
    expect(readDiscoverEmberPrompt("wpid_1")).toBe("First prompt");
  });

  it("reads a new stored prompt when the recovery workflow changes", async () => {
    rememberDiscoverEmberPrompt("wpid_1", "First prompt");
    rememberDiscoverEmberPrompt("wpid_2", "Second prompt");

    const { rerender } = render(
      createElement(RecoveryHarness, { workflowPermanentId: "wpid_1" }),
    );

    expect(screen.getByTestId("stored-prompt").textContent).toBe(
      "First prompt",
    );

    rerender(createElement(RecoveryHarness, { workflowPermanentId: "wpid_2" }));

    await waitFor(() => {
      expect(screen.getByTestId("stored-prompt").textContent).toBe(
        "Second prompt",
      );
    });
  });

  it("ignores a stored prompt when recovery reads are disabled", () => {
    rememberDiscoverEmberPrompt("wpid_1", "Stored prompt");

    render(
      createElement(RecoveryHarness, {
        shouldRead: false,
        workflowPermanentId: "wpid_1",
      }),
    );

    expect(screen.getByTestId("stored-prompt").textContent).toBe("");
    expect(readDiscoverEmberPrompt("wpid_1")).toBe("Stored prompt");
  });

  it("does not restore a consumed Discover prompt after a later Fix seed", async () => {
    rememberDiscoverEmberPrompt("wpid_1", "Discover seed");
    const sentMessages: string[] = [];

    render(createElement(InitialMessageHarness, { sentMessages }));

    await waitFor(() => {
      expect(sentMessages).toEqual(["Discover seed"]);
    });

    fireEvent.click(screen.getByRole("button", { name: "send fix seed" }));

    await waitFor(() => {
      expect(sentMessages).toEqual(["Discover seed", "Fix seed"]);
    });
    expect(readDiscoverEmberPrompt("wpid_1")).toBeNull();
  });

  it("does not resurrect a consumed prompt when the recovery hook remounts", () => {
    rememberDiscoverEmberPrompt("wpid_1", "Discover seed");

    const first = renderHook(() =>
      useDiscoverEmberPromptRecovery({
        shouldRead: true,
        workflowPermanentId: "wpid_1",
      }),
    );
    expect(first.result.current.storedInitialEmberMessage).toBe(
      "Discover seed",
    );

    act(() => {
      first.result.current.clearStoredInitialEmberMessage();
    });
    expect(first.result.current.storedInitialEmberMessage).toBeNull();
    expect(readDiscoverEmberPrompt("wpid_1")).toBeNull();
    first.unmount();

    // A fresh mount re-runs the useState initializer that reads sessionStorage;
    // clearing must have removed the entry so the consumed prompt stays gone.
    const second = renderHook(() =>
      useDiscoverEmberPromptRecovery({
        shouldRead: true,
        workflowPermanentId: "wpid_1",
      }),
    );
    expect(second.result.current.storedInitialEmberMessage).toBeNull();
    second.unmount();
  });
});

describe("shouldOpenEmberPaneForHandoff", () => {
  it("opens the Ember pane for a studio handoff with a seeded prompt", () => {
    expect(
      shouldOpenEmberPaneForHandoff({
        embedded: true,
        hasInitialEmberMessage: true,
        emberPaneOpen: false,
      }),
    ).toBe(true);
  });

  it("does not open outside the studio shell (non-embedded editor)", () => {
    expect(
      shouldOpenEmberPaneForHandoff({
        embedded: false,
        hasInitialEmberMessage: true,
        emberPaneOpen: false,
      }),
    ).toBe(false);
  });

  it("does not open when there is no seeded prompt", () => {
    expect(
      shouldOpenEmberPaneForHandoff({
        embedded: true,
        hasInitialEmberMessage: false,
        emberPaneOpen: false,
      }),
    ).toBe(false);
  });

  it("is a no-op when the Ember pane is already open", () => {
    expect(
      shouldOpenEmberPaneForHandoff({
        embedded: true,
        hasInitialEmberMessage: true,
        emberPaneOpen: true,
      }),
    ).toBe(false);
  });
});

describe("withoutDiscoverViaParam", () => {
  it("strips a via=discover param but keeps other params", () => {
    expect(withoutDiscoverViaParam("?via=discover&cache-key-value=abc")).toBe(
      "?cache-key-value=abc",
    );
  });

  it("drops the leading '?' when no params remain", () => {
    expect(withoutDiscoverViaParam("?via=discover")).toBe("");
  });

  it("leaves non-discover via values untouched", () => {
    expect(withoutDiscoverViaParam("?via=onboarding")).toBe("?via=onboarding");
  });

  it("is a no-op when there is no via param", () => {
    expect(withoutDiscoverViaParam("?cache-key-value=abc")).toBe(
      "?cache-key-value=abc",
    );
    expect(withoutDiscoverViaParam("")).toBe("");
  });
});

describe("handoff prompt storage keys", () => {
  it("reads the Ember key first, falls back to the old key, writes the Ember key, and forgets both", () => {
    sessionStorage.setItem("skyvern.discoverCopilotHandoff:wpid_1", "old");
    expect(readDiscoverEmberPrompt("wpid_1")).toBe("old");
    sessionStorage.setItem("skyvern.discoverEmberHandoff:wpid_1", "new");
    expect(readDiscoverEmberPrompt("wpid_1")).toBe("new");
    forgetDiscoverEmberPrompt("wpid_1");
    expect(sessionStorage.length).toBe(0);

    rememberDiscoverEmberPrompt("wpid_2", "seed");
    expect(sessionStorage.getItem("skyvern.discoverEmberHandoff:wpid_2")).toBe(
      "seed",
    );
    expect(
      sessionStorage.getItem("skyvern.discoverCopilotHandoff:wpid_2"),
    ).toBeNull();
  });
});
