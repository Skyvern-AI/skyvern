import { CrossCircledIcon } from "@radix-ui/react-icons";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { DebuggerRunTimelineMinimal } from "./DebuggerRunTimelineMinimal";
import { Tip } from "@/components/Tip";

function DebuggerRunMinimal() {
  const { data: workflowRun } = useWorkflowRunQuery();

  const workflowFailureReason = workflowRun?.failure_reason ? (
    <Tip content={workflowRun.failure_reason}>
      <div className="items-center-justify-center flex text-destructive">
        <CrossCircledIcon />
      </div>
    </Tip>
  ) : null;

  return (
    <div className="relative flex h-full w-full flex-col items-center justify-start gap-4 overflow-hidden overflow-y-auto pb-12 pt-4">
      {workflowFailureReason}
      <div className="flex h-full w-full items-start justify-center">
        <DebuggerRunTimelineMinimal />
      </div>
    </div>
  );
}

export { DebuggerRunMinimal };
