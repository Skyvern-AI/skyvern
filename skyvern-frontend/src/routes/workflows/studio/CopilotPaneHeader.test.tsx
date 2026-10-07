import { act, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { useCopilotHeaderStore } from "@/store/useCopilotHeaderStore";

import {
  CopilotActiveDot,
  CopilotPaneControls,
  CopilotPaneStatus,
} from "./CopilotPaneHeader";

const noop = () => {};

function registerControls(navigationLockedReason: string | null) {
  useCopilotHeaderStore.setState({
    controls: {
      workflowPermanentId: "wpid_1",
      currentChatId: "chat-1",
      onSelectChat: noop,
      onNewChat: noop,
      disabled: navigationLockedReason !== null,
      newChatDisabled: navigationLockedReason !== null,
      navigationLockedReason,
    },
  });
}

describe("CopilotPaneControls", () => {
  it("keeps a locked control's reason reachable instead of hanging it off the disabled button", () => {
    const reason = "Copilot couldn't confirm whether an Accept already saved.";
    registerControls(reason);
    render(
      <TooltipProvider>
        <CopilotPaneControls />
      </TooltipProvider>,
    );

    for (const name of [/^New chat/, /^History/]) {
      const button = screen.getByRole("button", { name });
      expect(button.matches(":disabled")).toBe(true);
      expect(button.getAttribute("aria-label")).toContain(reason);
      // A disabled button swallows the pointer and focus events its own tooltip
      // trigger needs, so the reason has to hang off a focusable wrapper.
      expect(button.closest("[tabindex='0']")).not.toBeNull();
    }
  });
});

describe("Copilot pane waiting signal", () => {
  it("carries a pending question on the status dot instead of a header label", () => {
    useCopilotHeaderStore.setState({ attention: "question" });
    render(
      <>
        <CopilotActiveDot />
        <CopilotPaneStatus />
      </>,
    );

    const dot = screen.getByRole("img", { name: "Copilot needs your answer" });
    expect(dot.getAttribute("title")).toBe("Copilot needs your answer");
    expect(dot.className).toContain("bg-amber-500");
    expect(
      screen.queryByText(/needs your answer/i, {
        selector: "span:not([role])",
      }),
    ).toBeNull();

    act(() => useCopilotHeaderStore.setState({ attention: null }));
    expect(
      screen.getByRole("img", { name: "Copilot session active" }).className,
    ).toContain("bg-success");
  });
});
