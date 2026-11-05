import { useContext } from "react";
import { Button } from "@/components/ui/button";
import type { AppNode } from "../nodes";
import { docOneActionStepWantsTask } from "./docOneAction";
import { WorkflowDocActionsContext } from "./WorkflowDocActionsContext";

// Shown before Run, under a Browser Action whose instruction needs several actions.
export function DocOneActionHint({ node }: { node: AppNode }) {
  const { edit } = useContext(WorkflowDocActionsContext);
  if (!edit || !docOneActionStepWantsTask(node)) return null;
  return (
    <div
      data-doc-one-action-hint
      className="mb-1 mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px] text-muted-foreground"
    >
      <span>
        This step does one action. Turn it into a Browser Task to do several?
      </span>
      <Button
        type="button"
        size="sm"
        variant="outline"
        className="h-6 px-2 text-[12.5px]"
        onClick={() => edit.turnInto(node.id, "navigation")}
      >
        Turn into Browser Task
      </Button>
    </div>
  );
}
