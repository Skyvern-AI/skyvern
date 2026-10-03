import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { useCopilotHeaderStore } from "@/store/useCopilotHeaderStore";

import { CopilotPaneControls } from "./CopilotPaneHeader";

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
