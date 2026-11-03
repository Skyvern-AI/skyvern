import { UploadIcon } from "@radix-ui/react-icons";
import { useRef } from "react";

import { TooltipProvider } from "@/components/ui/tooltip";
import { toast } from "@/components/ui/use-toast";
import { ControlTooltip } from "@/routes/workflows/studio/ControlTooltip";
import { cn } from "@/util/utils";

type UploadSopAction = {
  onFile: (file: File) => void;
  disabled?: boolean;
  /** Replaces the default hint, e.g. with the reason the action is unavailable. */
  tooltip?: string;
};

type RecordTaskAction = {
  onSelect: () => void;
  disabled?: boolean;
  tooltip?: string;
};

type StartFromCardsProps = {
  uploadSOP: UploadSopAction;
  recordTask: RecordTaskAction;
  className?: string;
  labelClassName?: string;
  /** Column rule for the host: the Copilot pane uses a container query, Home a breakpoint. */
  gridClassName?: string;
};

const UPLOAD_SOP_HINT = "Attach a procedure PDF for Copilot to build from";
const RECORD_TASK_HINT =
  "Skyvern records your clicks, typing, and navigation, then turns them into workflow steps";

const cardClassName =
  "flex w-full min-w-0 items-center gap-3 rounded-lg border border-border bg-slate-elevation3 p-3 text-left transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50";
const iconBoxClassName =
  "flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-border bg-slate-elevation2";

function StartFromCards({
  uploadSOP,
  recordTask,
  className,
  labelClassName,
  gridClassName,
}: StartFromCardsProps) {
  const sopFileInputRef = useRef<HTMLInputElement>(null);
  const uploadDisabled = Boolean(uploadSOP.disabled);
  const recordDisabled = Boolean(recordTask.disabled);

  return (
    <div className={className}>
      <p className={cn("text-xs text-muted-foreground/80", labelClassName)}>
        Or start from
      </p>
      <TooltipProvider>
        <div className={cn("mt-2 grid grid-cols-1 gap-2", gridClassName)}>
          <ControlTooltip
            content={uploadSOP.tooltip ?? UPLOAD_SOP_HINT}
            blocked={uploadDisabled}
            side="top"
            wrapperClassName="min-w-0 w-full"
          >
            <button
              type="button"
              aria-label="Upload an SOP"
              className={cardClassName}
              disabled={uploadDisabled}
              onClick={() => sopFileInputRef.current?.click()}
            >
              <span className={iconBoxClassName}>
                <UploadIcon className="h-4 w-4" />
              </span>
              <span className="min-w-0">
                <span className="block text-sm font-medium text-foreground">
                  Upload an SOP
                </span>
                <span className="mt-0.5 block text-xs leading-snug text-muted-foreground">
                  A PDF of the procedure
                </span>
              </span>
            </button>
          </ControlTooltip>
          <ControlTooltip
            content={recordTask.tooltip ?? RECORD_TASK_HINT}
            blocked={recordDisabled}
            side="top"
            wrapperClassName="min-w-0 w-full"
          >
            <button
              type="button"
              aria-label="Demonstrate task"
              className={cardClassName}
              disabled={recordDisabled}
              onClick={recordTask.onSelect}
            >
              <span className={iconBoxClassName}>
                <span className="h-2.5 w-2.5 rounded-full bg-red-500" />
              </span>
              <span className="min-w-0">
                <span className="block text-sm font-medium text-foreground">
                  Demonstrate task
                </span>
                <span className="mt-0.5 block text-xs leading-snug text-muted-foreground">
                  Show Copilot how in the browser
                </span>
              </span>
            </button>
          </ControlTooltip>
        </div>
      </TooltipProvider>
      <input
        ref={sopFileInputRef}
        type="file"
        accept=".pdf,application/pdf"
        aria-label="Choose an SOP PDF"
        className="hidden"
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (!file) return;
          // Reset first: picking the same file twice in a row fires no change event otherwise.
          event.target.value = "";
          if (uploadDisabled) return;
          if (!file.name.toLowerCase().endsWith(".pdf")) {
            toast({
              variant: "destructive",
              title: "Invalid file type",
              description: "Please select a PDF file",
            });
            return;
          }
          uploadSOP.onFile(file);
        }}
      />
    </div>
  );
}

export { StartFromCards };
