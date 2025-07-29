import { ReactFlowProvider } from "@xyflow/react";
import { useParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { BrowserStream } from "@/components/BrowserStream";
import { FloatingWindow } from "@/components/FloatingWindow";
import { Skeleton } from "@/components/ui/skeleton";
import { useMountEffect } from "@/hooks/useMountEffect";
import { statusIsFinalized } from "@/routes/tasks/types.ts";
import { useSidebarStore } from "@/store/SidebarStore";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { useWorkflowQuery } from "../hooks/useWorkflowQuery";
import { useDebugSessionQuery } from "../hooks/useDebugSessionQuery";
import { WorkflowSettings } from "../types/workflowTypes";
import { FlowRenderer } from "./FlowRenderer";
import { getElements } from "./workflowEditorUtils";
import { getInitialParameters } from "./utils";

function WorkflowDebugger() {
  const { workflowPermanentId } = useParams();
  const queryClient = useQueryClient();
  const [shouldFetchDebugSession, setShouldFetchDebugSession] = useState(false);

  const { data: workflowRun } = useWorkflowRunQuery();
  const { data: workflow } = useWorkflowQuery({
    workflowPermanentId,
  });

  const { data: debugSession } = useDebugSessionQuery({
    workflowPermanentId,
    enabled: shouldFetchDebugSession && !!workflowPermanentId,
  });

  const setCollapsed = useSidebarStore((state) => {
    return state.setCollapsed;
  });

  const setHasChanges = useWorkflowHasChangesStore(
    (state) => state.setHasChanges,
  );

  useMountEffect(() => {
    setCollapsed(true);
    setHasChanges(false);

    if (workflowPermanentId) {
      queryClient.removeQueries({
        queryKey: ["debugSession", workflowPermanentId],
      });
      setShouldFetchDebugSession(true);
    }
  });

  const intervalRef = useRef<NodeJS.Timeout | null>(null);

  useEffect(() => {
    if (
      (!debugSession || !debugSession.browser_session_id) &&
      shouldFetchDebugSession &&
      workflowPermanentId
    ) {
      intervalRef.current = setInterval(() => {
        queryClient.invalidateQueries({
          queryKey: ["debugSession", workflowPermanentId],
        });
      }, 2000);
    } else {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    }

    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
      }
    };
  }, [debugSession, shouldFetchDebugSession, workflowPermanentId, queryClient]);

  if (!workflow) {
    return null;
  }

  const settings: WorkflowSettings = {
    persistBrowserSession: workflow.persist_browser_session,
    proxyLocation: workflow.proxy_location,
    webhookCallbackUrl: workflow.webhook_callback_url,
    model: workflow.model,
    maxScreenshotScrolls: workflow.max_screenshot_scrolls,
    extraHttpHeaders: workflow.extra_http_headers
      ? JSON.stringify(workflow.extra_http_headers)
      : null,
  };

  const elements = getElements(
    workflow.workflow_definition.blocks,
    settings,
    true,
  );

  const isFinalized = workflowRun ? statusIsFinalized(workflowRun) : null;
  const interactor = workflowRun && isFinalized === false ? "agent" : "human";
  const browserTitle = interactor === "agent" ? `Browser [🤖]` : `Browser [👤]`;

  return (
    <div className="relative flex h-screen w-full">
      <ReactFlowProvider>
        <FlowRenderer
          initialEdges={elements.edges}
          initialNodes={elements.nodes}
          initialParameters={getInitialParameters(workflow)}
          initialTitle={workflow.title}
          workflow={workflow}
        />
      </ReactFlowProvider>

      {debugSession && (
        <FloatingWindow
          title={browserTitle}
          bounded={false}
          initialWidth={512}
          initialHeight={360}
          showMaximizeButton={true}
          showMinimizeButton={true}
          showReloadButton={true}
        >
          {debugSession && debugSession.browser_session_id ? (
            <BrowserStream
              interactive={interactor === "human"}
              browserSessionId={debugSession.browser_session_id}
            />
          ) : (
            <Skeleton className="h-full w-full" />
          )}
        </FloatingWindow>
      )}
    </div>
  );
}

export { WorkflowDebugger };
