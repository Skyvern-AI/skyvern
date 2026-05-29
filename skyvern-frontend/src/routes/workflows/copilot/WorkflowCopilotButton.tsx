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
      className="flex items-center gap-2 text-neutral-700 transition-colors hover:text-neutral-950 dark:text-neutral-300 dark:hover:text-white"
      title="Open Agent Copilot"
    >
      <ChatBubbleIcon className="h-4 w-4" />
      <span>Copilot</span>
      {messageCount > 0 && (
        <span className="flex h-5 w-5 items-center justify-center rounded-full bg-neutral-200 text-xs font-bold text-neutral-950 ring-1 ring-neutral-300/80 dark:bg-neutral-100 dark:ring-0">
          {messageCount}
        </span>
      )}
    </button>
  );
});

WorkflowCopilotButton.displayName = "WorkflowCopilotButton";
