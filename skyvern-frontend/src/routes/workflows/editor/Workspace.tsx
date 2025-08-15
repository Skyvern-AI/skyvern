import { AxiosError } from "axios";
import { useEffect, useRef, useState } from "react";
import { nanoid } from "nanoid";
import { ReloadIcon } from "@radix-ui/react-icons";
import { useParams } from "react-router-dom";
import { useEdgesState, useNodesState, Edge } from "@xyflow/react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { DebugSessionApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useMountEffect } from "@/hooks/useMountEffect";
import { useRanker } from "../hooks/useRanker";
import { useDebugSessionQuery } from "../hooks/useDebugSessionQuery";
import { useBlockScriptsQuery } from "@/routes/workflows/hooks/useBlockScriptsQuery";
import { useBlockScriptStore } from "@/store/BlockScriptStore";
import { useSidebarStore } from "@/store/SidebarStore";

import { AnimatedWave } from "@/components/AnimatedWave";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogClose,
} from "@/components/ui/dialog";
import { SwitchBar } from "@/components/SwitchBar";
import { toast } from "@/components/ui/use-toast";
import { BrowserStream } from "@/components/BrowserStream";
import { FloatingWindow } from "@/components/FloatingWindow";
import { statusIsFinalized } from "@/routes/tasks/types.ts";
import { DebuggerRun } from "@/routes/workflows/debugger/DebuggerRun";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { DebuggerRunOutput } from "@/routes/workflows/debugger/DebuggerRunOutput";
import { DebuggerPostRunParameters } from "@/routes/workflows/debugger/DebuggerPostRunParameters";
import { useDebugStore } from "@/store/useDebugStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import {
  useWorkflowHasChangesStore,
  useWorkflowSave,
} from "@/store/WorkflowHasChangesStore";

import { FlowRenderer, type FlowRendererProps } from "./FlowRenderer";
import { AppNode, isWorkflowBlockNode, WorkflowBlockNode } from "./nodes";
import { WorkflowNodeLibraryPanel } from "./panels/WorkflowNodeLibraryPanel";
import { WorkflowParametersPanel } from "./panels/WorkflowParametersPanel";
import { getWorkflowErrors } from "./workflowEditorUtils";
import { WorkflowHeader } from "./WorkflowHeader";
import {
  nodeAdderNode,
  createNode,
  defaultEdge,
  generateNodeLabel,
  layout,
  startNode,
} from "./workflowEditorUtils";

const Constants = {
  NewBrowserCooldown: 30000,
} as const;

type Props = Pick<FlowRendererProps, "initialTitle" | "workflow"> & {
  initialNodes: Array<AppNode>;
  initialEdges: Array<Edge>;
  showBrowser?: boolean;
};

export type AddNodeProps = {
  nodeType: NonNullable<WorkflowBlockNode["type"]>;
  previous: string | null;
  next: string | null;
  parent?: string;
  connectingEdgeType: string;
};

function Workspace({
  initialNodes,
  initialEdges,
  initialTitle,
  showBrowser = false,
  workflow,
}: Props) {
  const { blockLabel, workflowPermanentId, workflowRunId } = useParams();
  const [content, setContent] = useState("actions");
  const { workflowPanelState, setWorkflowPanelState, closeWorkflowPanel } =
    useWorkflowPanelStore();
  const debugStore = useDebugStore();
  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);
  const saveWorkflow = useWorkflowSave();

  const { data: workflowRun } = useWorkflowRunQuery();
  const isFinalized = workflowRun ? statusIsFinalized(workflowRun) : null;
  const interactor = workflowRun && isFinalized === false ? "agent" : "human";
  const browserTitle = interactor === "agent" ? `Browser [🤖]` : `Browser [👤]`;

  const [openDialogue, setOpenDialogue] = useState(false);
  const [activeDebugSession, setActiveDebugSession] =
    useState<DebugSessionApiResponse | null>(null);
  const [showPowerButton, setShowPowerButton] = useState(true);
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const [shouldFetchDebugSession, setShouldFetchDebugSession] = useState(false);
  const blockScriptStore = useBlockScriptStore();
  const { rankedItems, promote } = useRanker([
    "browserWindow",
    "header",
    "dropdown",
    "history",
    "infiniteCanvas",
  ]);

  // ---start fya: https://github.com/frontyardart
  const initialBrowserPosition = {
    x: 600,
    y: 132,
  };

  const windowWidth = window.innerWidth;
  const rightPadding = 567;
  const initialWidth = Math.max(
    512,
    windowWidth - initialBrowserPosition.x - rightPadding,
  );
  const initialHeight = (initialWidth / 16) * 9;
  // ---end fya

  const { data: blockScripts } = useBlockScriptsQuery({
    workflowPermanentId,
  });

  const { data: debugSession } = useDebugSessionQuery({
    workflowPermanentId,
    enabled: shouldFetchDebugSession && !!workflowPermanentId,
  });

  const setCollapsed = useSidebarStore((state) => {
    return state.setCollapsed;
  });

  const workflowChangesStore = useWorkflowHasChangesStore();

  const handleOnCycle = () => {
    setOpenDialogue(true);
  };

  useMountEffect(() => {
    setCollapsed(true);
    workflowChangesStore.setHasChanges(false);

    if (workflowPermanentId) {
      queryClient.removeQueries({
        queryKey: ["debugSession", workflowPermanentId],
      });
      setShouldFetchDebugSession(true);
    }
  });

  useEffect(() => {
    blockScriptStore.setScripts(blockScripts ?? {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [blockScripts]);

  const afterCycleBrowser = () => {
    setOpenDialogue(false);
    setShowPowerButton(false);

    if (powerButtonTimeoutRef.current) {
      clearTimeout(powerButtonTimeoutRef.current);
    }

    powerButtonTimeoutRef.current = setTimeout(() => {
      setShowPowerButton(true);
    }, Constants.NewBrowserCooldown);
  };

  const cycleBrowser = useMutation({
    mutationFn: async (id: string) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client.post<DebugSessionApiResponse>(`/debug-session/${id}/new`);
    },
    onSuccess: (response) => {
      const newDebugSession = response.data;
      setActiveDebugSession(newDebugSession);

      queryClient.invalidateQueries({
        queryKey: ["debugSession", workflowPermanentId],
      });

      toast({
        title: "Browser cycled",
        variant: "success",
        description: "Your browser has been cycled.",
      });

      afterCycleBrowser();
    },
    onError: (error: AxiosError) => {
      toast({
        variant: "destructive",
        title: "Failed to cycle browser",
        description: error.message,
      });

      afterCycleBrowser();
    },
  });

  const intervalRef = useRef<NodeJS.Timeout | null>(null);
  const powerButtonTimeoutRef = useRef<NodeJS.Timeout | null>(null);

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

      if (debugSession) {
        setActiveDebugSession(debugSession);
      }
    }

    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
      }
    };
  }, [debugSession, shouldFetchDebugSession, workflowPermanentId, queryClient]);

  function doLayout(nodes: Array<AppNode>, edges: Array<Edge>) {
    const layoutedElements = layout(nodes, edges);
    setNodes(layoutedElements.nodes);
    setEdges(layoutedElements.edges);
  }

  function addNode({
    nodeType,
    previous,
    next,
    parent,
    connectingEdgeType,
  }: AddNodeProps) {
    const newNodes: Array<AppNode> = [];
    const newEdges: Array<Edge> = [];
    const id = nanoid();
    const existingLabels = nodes
      .filter(isWorkflowBlockNode)
      .map((node) => node.data.label);
    const node = createNode(
      { id, parentId: parent },
      nodeType,
      generateNodeLabel(existingLabels),
    );
    newNodes.push(node);
    if (previous) {
      const newEdge = {
        id: nanoid(),
        type: "edgeWithAddButton",
        source: previous,
        target: id,
        style: {
          strokeWidth: 2,
        },
      };
      newEdges.push(newEdge);
    }
    if (next) {
      const newEdge = {
        id: nanoid(),
        type: connectingEdgeType,
        source: id,
        target: next,
        style: {
          strokeWidth: 2,
        },
      };
      newEdges.push(newEdge);
    }

    if (nodeType === "loop") {
      // when loop node is first created it needs an adder node so nodes can be added inside the loop
      const startNodeId = nanoid();
      const adderNodeId = nanoid();
      newNodes.push(
        startNode(
          startNodeId,
          {
            withWorkflowSettings: false,
            editable: true,
          },
          id,
        ),
      );
      newNodes.push(nodeAdderNode(adderNodeId, id));
      newEdges.push(defaultEdge(startNodeId, adderNodeId));
    }

    const editedEdges = previous
      ? edges.filter((edge) => edge.source !== previous)
      : edges;

    const previousNode = nodes.find((node) => node.id === previous);
    const previousNodeIndex = previousNode
      ? nodes.indexOf(previousNode)
      : nodes.length - 1;

    // creating some memory for no reason, maybe check it out later
    const newNodesAfter = [
      ...nodes.slice(0, previousNodeIndex + 1),
      ...newNodes,
      ...nodes.slice(previousNodeIndex + 1),
    ];

    workflowChangesStore.setHasChanges(true);
    doLayout(newNodesAfter, [...editedEdges, ...newEdges]);
  }

  return (
    <div className="relative h-full w-full">
      <Dialog
        open={openDialogue}
        onOpenChange={(open) => {
          if (!open && cycleBrowser.isPending) {
            return;
          }
          setOpenDialogue(open);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Cycle (Get a new browser)</DialogTitle>
            <DialogDescription>
              <div className="pb-2 pt-4 text-sm text-slate-400">
                {cycleBrowser.isPending ? (
                  <>
                    Cooking you up a fresh browser...
                    <AnimatedWave text=".‧₊˚ ⋅ ✨★ ‧₊˚ ⋅" />
                  </>
                ) : (
                  "Abandon this browser for a new one. Are you sure?"
                )}
              </div>
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            {!cycleBrowser.isPending && (
              <DialogClose asChild>
                <Button variant="secondary">Cancel</Button>
              </DialogClose>
            )}
            <Button
              variant="default"
              onClick={() => {
                cycleBrowser.mutate(workflowPermanentId!);
              }}
              disabled={cycleBrowser.isPending}
            >
              Yes, Continue{" "}
              {cycleBrowser.isPending && (
                <ReloadIcon className="ml-2 size-4 animate-spin" />
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* header panel */}
      <div
        className="absolute left-6 right-6 top-8 h-20"
        style={{ zIndex: rankedItems.header ?? 3 }}
        onMouseDownCapture={() => {
          promote("header");
        }}
      >
        <WorkflowHeader
          saving={workflowChangesStore.saveIsPending}
          parametersPanelOpen={
            workflowPanelState.active &&
            workflowPanelState.content === "parameters"
          }
          onParametersClick={() => {
            if (
              workflowPanelState.active &&
              workflowPanelState.content === "parameters"
            ) {
              closeWorkflowPanel();
              promote("header");
            } else {
              setWorkflowPanelState({
                active: true,
                content: "parameters",
              });
              promote("dropdown");
            }
          }}
          onSave={async () => {
            const errors = getWorkflowErrors(nodes);
            if (errors.length > 0) {
              toast({
                title: "Can not save workflow because of errors:",
                description: (
                  <div className="space-y-2">
                    {errors.map((error) => (
                      <p key={error}>{error}</p>
                    ))}
                  </div>
                ),
                variant: "destructive",
              });
              return;
            }
            await saveWorkflow.mutateAsync();
          }}
          onRun={() => {
            closeWorkflowPanel();
            promote("header");
          }}
        />
      </div>

      {/* sub panels */}
      {workflowPanelState.active && (
        <div
          className="absolute right-6 top-[7.75rem]"
          style={{ zIndex: rankedItems.dropdown ?? 2 }}
          onMouseDownCapture={() => {
            promote("dropdown");
          }}
        >
          {workflowPanelState.content === "parameters" && (
            <WorkflowParametersPanel
              onMouseDownCapture={() => {
                promote("dropdown");
              }}
            />
          )}
          {workflowPanelState.content === "nodeLibrary" && (
            <WorkflowNodeLibraryPanel
              onMouseDownCapture={() => {
                promote("dropdown");
              }}
              onNodeClick={(props) => {
                addNode(props);
              }}
            />
          )}
        </div>
      )}

      {debugStore.isDebugMode && (
        <div
          className="absolute right-6 top-[8.5rem] h-[calc(100vh-9.5rem)]"
          style={{ zIndex: rankedItems.history ?? 1 }}
          onMouseDownCapture={() => {
            closeWorkflowPanel();
            promote("history");
          }}
        >
          <div className="pointer-events-none absolute right-0 top-0 flex h-full w-[400px] flex-col items-end justify-end bg-slate-900">
            <div className="pointer-events-auto relative flex h-full w-full flex-col items-start overflow-hidden rounded-xl border-2 border-slate-500">
              {workflowRunId && (
                <SwitchBar
                  className="m-2 border-none"
                  onChange={(value) => setContent(value)}
                  value={content}
                  options={[
                    {
                      label: "Actions",
                      value: "actions",
                    },
                    {
                      label: "Inputs",
                      value: "inputs",
                    },
                    {
                      label: "Outputs",
                      value: "outputs",
                    },
                  ]}
                />
              )}
              <div className="h-full w-full overflow-hidden overflow-y-auto">
                {(!workflowRunId || content === "actions") && <DebuggerRun />}
                {workflowRunId && content === "inputs" && (
                  <DebuggerPostRunParameters />
                )}
                {workflowRunId && content === "outputs" && (
                  <DebuggerRunOutput />
                )}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* infinite canvas */}
      <FlowRenderer
        nodes={nodes}
        edges={edges}
        setNodes={setNodes}
        setEdges={setEdges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        initialTitle={initialTitle}
        // initialParameters={initialParameters}
        workflow={workflow}
        onMouseDownCapture={() => promote("infiniteCanvas")}
        zIndex={rankedItems.infiniteCanvas}
      />

      {/* browser */}
      {showBrowser && (
        <FloatingWindow
          title={browserTitle}
          bounded={false}
          initialPosition={initialBrowserPosition}
          initialWidth={initialWidth}
          initialHeight={initialHeight}
          showMaximizeButton={true}
          showMinimizeButton={true}
          showPowerButton={blockLabel === undefined && showPowerButton}
          showReloadButton={true}
          zIndex={rankedItems.browserWindow ?? 4}
          // --
          onCycle={handleOnCycle}
          onFocus={() => promote("browserWindow")}
        >
          {activeDebugSession &&
          activeDebugSession.browser_session_id &&
          !cycleBrowser.isPending ? (
            <BrowserStream
              interactive={interactor === "human"}
              browserSessionId={activeDebugSession.browser_session_id}
            />
          ) : (
            <div className="flex h-full w-full flex-col items-center justify-center gap-2 pb-2 pt-4 text-sm text-slate-400">
              Connecting to your browser...
              <AnimatedWave text=".‧₊˚ ⋅ ✨★ ‧₊˚ ⋅" />
            </div>
          )}
        </FloatingWindow>
      )}
    </div>
  );
}

export { Workspace };
