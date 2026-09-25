import { Cross2Icon, FileIcon, Pencil1Icon } from "@radix-ui/react-icons";
import type { ReactNode } from "react";

import { CornerDownRightIcon } from "@/components/icons/CornerDownRightIcon";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ControlTooltip } from "@/routes/workflows/studio/ControlTooltip";

import type { CopilotAttachedFile } from "./workflowCopilotTypes";

type Props = {
  text: string;
  attachments: CopilotAttachedFile[];
  // Omitted for a product action's receipt, which has no words of the user's to edit.
  onEdit?: () => void;
  onRemove: () => void;
};

export function QueuedMessageStrip({
  text,
  attachments,
  onEdit,
  onRemove,
}: Props) {
  return (
    <div
      data-testid="copilot-queued-message"
      className="group/queued flex items-center gap-2 rounded-t-lg border border-b-0 border-input bg-slate-elevation1 py-[3px] pl-2.5 pr-1 text-[12.5px] text-muted-foreground transition-colors duration-200 animate-in fade-in slide-in-from-bottom-2 motion-reduce:animate-none [&:has(+[role=group]:focus-within)]:border-ring"
    >
      <CornerDownRightIcon className="h-3 w-3 shrink-0" />
      <span className="min-w-0 flex-1 truncate" title={text}>
        {text}
      </span>
      {attachments.length > 0 ? (
        <span
          className="flex shrink-0 items-center gap-1"
          title={attachments.map((item) => item.filename).join(", ")}
        >
          <FileIcon aria-hidden className="h-3 w-3" />
          <span className="max-w-24 truncate">
            {attachments.length === 1
              ? attachments[0]!.filename
              : `${attachments.length} files`}
          </span>
        </span>
      ) : null}
      <TooltipProvider>
        <span className="flex shrink-0 opacity-55 transition-opacity group-focus-within/queued:opacity-100 group-hover/queued:opacity-100">
          {onEdit ? (
            <StripButton label="Edit queued message" onClick={onEdit}>
              <Pencil1Icon className="h-3 w-3" />
            </StripButton>
          ) : null}
          <StripButton label="Remove queued message" onClick={onRemove}>
            <Cross2Icon className="h-3 w-3" />
          </StripButton>
        </span>
      </TooltipProvider>
    </div>
  );
}

function StripButton({
  label,
  onClick,
  children,
}: {
  label: string;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <ControlTooltip content={label} side="top">
      <button
        type="button"
        aria-label={label}
        onClick={onClick}
        className="grid h-6 w-6 place-items-center rounded text-muted-foreground hover:bg-slate-elevation3 hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      >
        {children}
      </button>
    </ControlTooltip>
  );
}
