import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { WorkflowDebuggerRunTimeline } from "./WorkflowDebuggerRunTimeline";

function WorkflowDebuggerRun() {
  const { data: workflowRun } = useWorkflowRunQuery();

  const workflowFailureReason = workflowRun?.failure_reason ? (
    <div
      className="m-4 w-full rounded-md border border-red-600 p-4"
      style={{
        backgroundColor: "rgba(220, 38, 38, 0.10)",
        width: "calc(100% - 2rem)",
      }}
    >
      <div className="font-bold">Workflow Failure Reason</div>
      <div className="text-sm">{workflowRun.failure_reason}</div>
    </div>
  ) : null;

  return (
    <div className="flex h-full w-full flex-col items-center justify-start overflow-hidden overflow-y-auto">
      <div className="flex h-full w-full flex-col items-center justify-start gap-4 bg-[#0c1121]">
        {workflowFailureReason}
        <div className="h-full w-full">
          <WorkflowDebuggerRunTimeline
            activeItem="stream"
            onActionItemSelected={() => {}}
            onBlockItemSelected={() => {}}
            onObserverThoughtCardSelected={() => {}}
          />
        </div>
      </div>
    </div>
  );
}

export { WorkflowDebuggerRun };
