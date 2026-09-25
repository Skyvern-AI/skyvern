import { apiWorkflowToSettings } from "@/routes/workflows/editor/apiWorkflowToSettings";
import { ReactFlowProvider } from "@xyflow/react";
import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";
import { useStudioRunId } from "../studio/useStudioRunId";
import { useWorkflowQuery } from "../hooks/useWorkflowQuery";
import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";
import { getElements } from "./workflowEditorUtils";
import { LogoMinimized } from "@/components/LogoMinimized";
import { useGlobalWorkflowsQuery } from "../hooks/useGlobalWorkflowsQuery";
import { useBlockOutputStore } from "@/store/BlockOutputStore";
import { useHydrateWorkflowParameters } from "@/store/WorkflowHasChangesStore";
import { StudioShell } from "../studio/StudioShell";
import { Workspace } from "./Workspace";
import { ProductTour } from "@/components/onboarding/ProductTour";
import { useProductTourShortcut } from "@/hooks/useProductTourShortcut";
import { useMountEffect } from "@/hooks/useMountEffect";
import { useWorkflowStudioEnabled } from "@/hooks/useWorkflowStudioEnabled";
import { useViaEntryPointCapture } from "../hooks/useViaEntryPointCapture";

function WorkflowEditor() {
  const workflowPermanentId = useWorkflowPermanentId();
  const studioEnabled = useWorkflowStudioEnabled();
  const {
    data: fetchedWorkflow,
    isLoading,
    isError: workflowQueryFailed,
  } = useWorkflowQuery({
    workflowPermanentId,
  });

  // Runs outlive their agent: the workflow GET 404s once the agent is deleted,
  // but a run deep link (?wr= or the /runs/{wr} path) can still be served from
  // the run's embedded workflow snapshot (the same query the run panes use), in
  // a read-only degraded mode.
  const deepLinkRunId = useStudioRunId();
  const { data: fallbackRun, isLoading: fallbackRunIsLoading } =
    useWorkflowRunWithWorkflowQuery({
      workflowRunId: studioEnabled ? deepLinkRunId : undefined,
    });
  const deletedWorkflowSnapshot =
    studioEnabled && workflowQueryFailed && fallbackRun?.workflow?.deleted_at
      ? fallbackRun.workflow
      : undefined;
  const effectiveWorkflow =
    (fetchedWorkflow?.workflow_permanent_id === workflowPermanentId
      ? fetchedWorkflow
      : undefined) ?? deletedWorkflowSnapshot;

  const { data: globalWorkflows, isLoading: isGlobalWorkflowsLoading } =
    useGlobalWorkflowsQuery();

  const blockOutputStore = useBlockOutputStore();

  useProductTourShortcut();

  useMountEffect(() => blockOutputStore.reset());

  useViaEntryPointCapture();

  useHydrateWorkflowParameters(effectiveWorkflow, workflowPermanentId);

  const awaitingRunFallback =
    studioEnabled &&
    workflowQueryFailed &&
    Boolean(deepLinkRunId) &&
    fallbackRunIsLoading;
  if (
    isLoading ||
    isGlobalWorkflowsLoading ||
    awaitingRunFallback ||
    (fetchedWorkflow &&
      fetchedWorkflow.workflow_permanent_id !== workflowPermanentId)
  ) {
    return (
      <div className="flex h-screen w-full items-center justify-center">
        <div className="animate-pulse">
          <LogoMinimized />
        </div>
      </div>
    );
  }

  if (!effectiveWorkflow) {
    return null;
  }
  const workflow = effectiveWorkflow;
  const workflowDeleted = Boolean(workflow.deleted_at);

  const isGlobalWorkflow = globalWorkflows?.some(
    (globalWorkflow) =>
      globalWorkflow.workflow_permanent_id === workflowPermanentId,
  );

  // getElements derives display routing (sequential defaulting + validation); the stored blocks are passed through unchanged.
  const blocksToRender = workflow.workflow_definition.blocks;

  const settings = apiWorkflowToSettings(workflow);

  const elements = getElements(
    blocksToRender,
    settings,
    !isGlobalWorkflow && !workflowDeleted,
  );

  return (
    <div className="relative flex h-screen w-full flex-col">
      {elements.validationError ? (
        <div
          role="alert"
          className="z-10 border-b border-amber-300 bg-amber-100 px-4 py-2 text-sm text-amber-700 dark:border-amber-700/40 dark:bg-amber-950/50 dark:text-amber-200"
        >
          <strong className="font-semibold">
            Workflow validation warning:
          </strong>{" "}
          {elements.validationError.message}
        </div>
      ) : null}
      <div className="relative flex min-h-0 flex-1">
        <ReactFlowProvider>
          {studioEnabled ? (
            <StudioShell
              key={workflowPermanentId}
              initialEdges={elements.edges}
              initialNodes={elements.nodes}
              initialTitle={workflow.title}
              workflow={workflow}
            />
          ) : (
            <Workspace
              key={workflowPermanentId}
              initialEdges={elements.edges}
              initialNodes={elements.nodes}
              initialTitle={workflow.title}
              showBrowser={false}
              workflow={workflow}
            />
          )}
        </ReactFlowProvider>
      </div>
      <ProductTour />
    </div>
  );
}

export { WorkflowEditor };
