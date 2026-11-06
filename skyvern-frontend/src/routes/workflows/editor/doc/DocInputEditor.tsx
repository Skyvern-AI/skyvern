import { type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverAnchor,
  PopoverContent,
} from "@/components/ui/popover";
import { WorkflowEditorParameterType } from "../../types/workflowTypes";
import type { ParametersState } from "../types";
import { WorkflowParameterEditPanel } from "../panels/WorkflowParameterEditPanel";

/** The Doc input popover uses the Visual editor so credential sources stay identical. */
export function DocInputEditor({
  open,
  type,
  initialValues,
  isNew = false,
  onSave,
  onDelete,
  onClose,
  children,
}: {
  open: boolean;
  type: WorkflowEditorParameterType;
  initialValues?: ParametersState[number];
  /** A just-created placeholder input: titled and validated as an Add. */
  isNew?: boolean;
  onSave: (parameter: ParametersState[number]) => void;
  onDelete?: () => void;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <Popover open={open} onOpenChange={(next) => !next && onClose()}>
      <PopoverAnchor asChild>{children}</PopoverAnchor>
      {open && (
        <PopoverContent
          align="start"
          collisionPadding={16}
          aria-label={initialValues && !isNew ? "Edit input" : "Add input"}
          className="max-h-[var(--radix-popover-content-available-height)] w-[min(352px,calc(100vw-32px))] overflow-y-auto rounded-[9px] p-2"
          onCloseAutoFocus={(event) => event.preventDefault()}
        >
          <WorkflowParameterEditPanel
            type={type}
            initialValues={initialValues}
            isNew={isNew}
            onSave={onSave}
            onClose={onClose}
          />
          {onDelete && (
            <div className="px-5 pb-3 pt-1">
              <Button
                variant="outline"
                className="h-auto px-2 py-1 text-xs text-destructive hover:text-destructive"
                onClick={onDelete}
              >
                Delete input
              </Button>
            </div>
          )}
        </PopoverContent>
      )}
    </Popover>
  );
}
