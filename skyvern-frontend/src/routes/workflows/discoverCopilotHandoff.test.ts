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
  forgetDiscoverCopilotPrompt,
  readDiscoverCopilotPrompt,
  rememberDiscoverCopilotPrompt,
  shouldOpenCopilotPaneForHandoff,
  useDiscoverCopilotPromptRecovery,
  withoutDiscoverViaParam,
} from "./discoverCopilotHandoff";

afterEach(() => {
  cleanup();
  sessionStorage.clear();
});

function InitialMessageHarness({ sentMessages }: { sentMessages: string[] }) {
  const [routeMessage, setRouteMessage] = useState<string | null>(
    "Discover seed",
  );
  const { storedInitialCopilotMessage, clearStoredInitialCopilotMessage } =
    useDiscoverCopilotPromptRecovery({
      shouldRead: true,
      workflowPermanentId: "wpid_1",
    });
  const initialMessage = useMemo(
    () => routeMessage ?? storedInitialCopilotMessage,
    [routeMessage, storedInitialCopilotMessage],
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
    clearStoredInitialCopilotMessage();
    setRouteMessage(null);
  }, [clearStoredInitialCopilotMessage, initialMessage, sentMessages]);

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
  const { storedInitialCopilotMessage, clearStoredInitialCopilotMessage } =
    useDiscoverCopilotPromptRecovery({
      shouldRead,
      workflowPermanentId,
    });

  return createElement(
    "div",
    null,
    createElement(
      "span",
      { "data-testid": "stored-prompt" },
      storedInitialCopilotMessage ?? "",
    ),
    createElement(
      "button",
      { onClick: clearStoredInitialCopilotMessage, type: "button" },
      "clear stored prompt",
    ),
  );
}

describe("discoverCopilotHandoff", () => {
  it("stores and clears the prompt for one workflow", () => {
    rememberDiscoverCopilotPrompt("wpid_1", "Build the workflow");

    expect(readDiscoverCopilotPrompt("wpid_1")).toBe("Build the workflow");
    expect(readDiscoverCopilotPrompt("wpid_1")).toBe("Build the workflow");

    forgetDiscoverCopilotPrompt("wpid_1");
    expect(readDiscoverCopilotPrompt("wpid_1")).toBeNull();
  });

  it("keeps prompts scoped by workflow id", () => {
    rememberDiscoverCopilotPrompt("wpid_1", "First prompt");
    rememberDiscoverCopilotPrompt("wpid_2", "Second prompt");

    expect(readDiscoverCopilotPrompt("wpid_2")).toBe("Second prompt");
    expect(readDiscoverCopilotPrompt("wpid_1")).toBe("First prompt");
  });

  it("reads a new stored prompt when the recovery workflow changes", async () => {
    rememberDiscoverCopilotPrompt("wpid_1", "First prompt");
    rememberDiscoverCopilotPrompt("wpid_2", "Second prompt");

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
    rememberDiscoverCopilotPrompt("wpid_1", "Stored prompt");

    render(
      createElement(RecoveryHarness, {
        shouldRead: false,
        workflowPermanentId: "wpid_1",
      }),
    );

    expect(screen.getByTestId("stored-prompt").textContent).toBe("");
    expect(readDiscoverCopilotPrompt("wpid_1")).toBe("Stored prompt");
  });

  it("does not restore a consumed Discover prompt after a later Fix seed", async () => {
    rememberDiscoverCopilotPrompt("wpid_1", "Discover seed");
    const sentMessages: string[] = [];

    render(createElement(InitialMessageHarness, { sentMessages }));

    await waitFor(() => {
      expect(sentMessages).toEqual(["Discover seed"]);
    });

    fireEvent.click(screen.getByRole("button", { name: "send fix seed" }));

    await waitFor(() => {
      expect(sentMessages).toEqual(["Discover seed", "Fix seed"]);
    });
    expect(readDiscoverCopilotPrompt("wpid_1")).toBeNull();
  });

  it("does not resurrect a consumed prompt when the recovery hook remounts", () => {
    rememberDiscoverCopilotPrompt("wpid_1", "Discover seed");

    const first = renderHook(() =>
      useDiscoverCopilotPromptRecovery({
        shouldRead: true,
        workflowPermanentId: "wpid_1",
      }),
    );
    expect(first.result.current.storedInitialCopilotMessage).toBe(
      "Discover seed",
    );

    act(() => {
      first.result.current.clearStoredInitialCopilotMessage();
    });
    expect(first.result.current.storedInitialCopilotMessage).toBeNull();
    expect(readDiscoverCopilotPrompt("wpid_1")).toBeNull();
    first.unmount();

    // A fresh mount re-runs the useState initializer that reads sessionStorage;
    // clearing must have removed the entry so the consumed prompt stays gone.
    const second = renderHook(() =>
      useDiscoverCopilotPromptRecovery({
        shouldRead: true,
        workflowPermanentId: "wpid_1",
      }),
    );
    expect(second.result.current.storedInitialCopilotMessage).toBeNull();
    second.unmount();
  });
});

describe("shouldOpenCopilotPaneForHandoff", () => {
  it("opens the Copilot pane for a studio handoff with a seeded prompt", () => {
    expect(
      shouldOpenCopilotPaneForHandoff({
        embedded: true,
        hasInitialCopilotMessage: true,
        copilotPaneOpen: false,
      }),
    ).toBe(true);
  });

  it("does not open outside the studio shell (non-embedded editor)", () => {
    expect(
      shouldOpenCopilotPaneForHandoff({
        embedded: false,
        hasInitialCopilotMessage: true,
        copilotPaneOpen: false,
      }),
    ).toBe(false);
  });

  it("does not open when there is no seeded prompt", () => {
    expect(
      shouldOpenCopilotPaneForHandoff({
        embedded: true,
        hasInitialCopilotMessage: false,
        copilotPaneOpen: false,
      }),
    ).toBe(false);
  });

  it("is a no-op when the Copilot pane is already open", () => {
    expect(
      shouldOpenCopilotPaneForHandoff({
        embedded: true,
        hasInitialCopilotMessage: true,
        copilotPaneOpen: true,
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
