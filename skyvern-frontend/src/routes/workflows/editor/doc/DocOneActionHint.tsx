import { Cross2Icon } from "@radix-ui/react-icons";
import { useContext, useState } from "react";
import { Button } from "@/components/ui/button";
import type { AppNode } from "../nodes";
import { docOneActionStepWantsTask } from "./docOneAction";
import { WorkflowDocActionsContext } from "./WorkflowDocActionsContext";

// Dismissals outlive the component: switching an If branch unmounts its steps' hints.
const dismissedNodeIds = new Set<string>();

// Shown under a Browser Action whose instruction needs several actions.
export function DocOneActionHint({ node }: { node: AppNode }) {
  const { edit } = useContext(WorkflowDocActionsContext);
  const [, rerender] = useState(0);
  if (
    dismissedNodeIds.has(node.id) ||
    !edit ||
    !docOneActionStepWantsTask(node)
  )
    return null;
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
      <Button
        type="button"
        size="icon"
        variant="ghost"
        className="size-6"
        aria-label="Dismiss"
        onClick={() => {
          dismissedNodeIds.add(node.id);
          rerender((n) => n + 1);
        }}
      >
        <Cross2Icon className="size-3.5" />
      </Button>
    </div>
  );
}
