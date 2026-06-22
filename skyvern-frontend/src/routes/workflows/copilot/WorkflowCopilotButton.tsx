import { forwardRef } from "react";
import { ChatBubbleIcon } from "@radix-ui/react-icons";

interface WorkflowCopilotButtonProps {
  messageCount: number;
  onClick: () => void;
}

export const WorkflowCopilotButton = forwardRef<
  HTMLButtonElement,
  WorkflowCopilotButtonProps
>(({ messageCount, onClick }, ref) => {
  return (
    <button
      ref={ref}
      onClick={onClick}
      className="flex items-center gap-2 text-muted-foreground transition-colors hover:text-foreground"
      title="Open Agent Copilot"
    >
      <ChatBubbleIcon className="h-4 w-4" />
      <span>Copilot</span>
      {messageCount > 0 && (
        <span className="flex h-5 w-5 items-center justify-center rounded-full bg-cta text-xs font-bold text-cta-foreground">
          {messageCount}
        </span>
      )}
    </button>
  );
});

WorkflowCopilotButton.displayName = "WorkflowCopilotButton";
