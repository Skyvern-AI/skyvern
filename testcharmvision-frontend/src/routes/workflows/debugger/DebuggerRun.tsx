import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { DebuggerRunTimeline } from "./DebuggerRunTimeline";

function DebuggerRun() {
  const { data: workflowRun } = useWorkflowRunQuery();

  const workflowFailureReason = workflowRun?.failure_reason ? (
    <div
      className="align-self-start h-[8rem] min-h-[8rem] w-full overflow-y-auto rounded-md border border-red-600 p-4"
      style={{
        backgroundColor: "rgba(220, 38, 38, 0.10)",
        width: "calc(100% - 2rem)",
      }}
    >
      <div className="font-bold">Run Failure Reason</div>
      <div className="text-sm">{workflowRun.failure_reason}</div>
    </div>
  ) : null;

  return (
    <div className="flex h-full w-full flex-col items-center justify-start overflow-hidden overflow-y-auto">
      {workflowFailureReason}
      <div className="h-full w-full">
        <DebuggerRunTimeline
          activeItem="stream"
          onActionItemSelected={() => {}}
          onBlockItemSelected={() => {}}
          onObserverThoughtCardSelected={() => {}}
        />
      </div>
    </div>
  );
}

export { DebuggerRun };
