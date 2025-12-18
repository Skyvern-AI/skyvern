import { AxiosError } from "axios";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  MutableRefObject,
} from "react";
import { nanoid } from "nanoid";
import {
  CheckIcon,
  ChevronRightIcon,
  ChevronLeftIcon,
  CopyIcon,
  GlobeIcon,
  PlayIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { useParams, useSearchParams } from "react-router-dom";
import {
  useEdgesState,
  useNodesState,
  useReactFlow,
  Edge,
} from "@xyflow/react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { DebugSessionApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useMountEffect } from "@/hooks/useMountEffect";
import { useDebugSessionQuery } from "../hooks/useDebugSessionQuery";
import { useBlockScriptsQuery } from "@/routes/workflows/hooks/useBlockScriptsQuery";
import { WorkflowRunStream } from "@/routes/workflows/workflowRun/WorkflowRunStream";
import { useCacheKeyValuesQuery } from "../hooks/useCacheKeyValuesQuery";
import { useBlockScriptStore } from "@/store/BlockScriptStore";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useSidebarStore } from "@/store/SidebarStore";

import { AnimatedWave } from "@/components/AnimatedWave";
import { Button } from "@/components/ui/button";
import {
  BreakoutButton,
  PowerButton,
  ReloadButton,
} from "@/components/FloatingWindow";
import { Splitter } from "@/components/Splitter";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { toast } from "@/components/ui/use-toast";
import { BrowserStream } from "@/components/BrowserStream";
import { statusIsFinalized } from "@/routes/tasks/types.ts";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { DebuggerRun } from "@/routes/workflows/debugger/DebuggerRun";
import { DebuggerRunMinimal } from "@/routes/workflows/debugger/DebuggerRunMinimal";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import {
  BranchContext,
  useWorkflowPanelStore,
} from "@/store/WorkflowPanelStore";
import {
  useWorkflowHasChangesStore,
  useWorkflowSave,
} from "@/store/WorkflowHasChangesStore";
import { getCode, getOrderedBlockLabels } from "@/routes/workflows/utils";
import { DebuggerBlockRuns } from "@/routes/workflows/debugger/DebuggerBlockRuns";
import { cn } from "@/util/utils";

import { FlowRenderer, type FlowRendererProps } from "./FlowRenderer";
import { AppNode, isWorkflowBlockNode, WorkflowBlockNode } from "./nodes";
import { ConditionalNodeData } from "./nodes/ConditionalNode/types";
import { WorkflowNodeLibraryPanel } from "./panels/WorkflowNodeLibraryPanel";
import { WorkflowParametersPanel } from "./panels/WorkflowParametersPanel";
import { WorkflowCacheKeyValuesPanel } from "./panels/WorkflowCacheKeyValuesPanel";
import { WorkflowComparisonPanel } from "./panels/WorkflowComparisonPanel";
import { getWorkflowErrors, getElements } from "./workflowEditorUtils";
import { WorkflowHeader } from "./WorkflowHeader";
import { WorkflowHistoryPanel } from "./panels/WorkflowHistoryPanel";
import { WorkflowVersion } from "../hooks/useWorkflowVersionsQuery";
import { WorkflowSettings } from "../types/workflowTypes";
import { ProxyLocation } from "@/api/types";
import {
  nodeAdderNode,
  createNode,
  defaultEdge,
  generateNodeLabel,
  layout,
  startNode,
} from "./workflowEditorUtils";
import { constructCacheKeyValue } from "./utils";
import "./workspace-styles.css";

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
  branch?: BranchContext;
};

interface Dom {
  splitLeft: MutableRefObject<HTMLInputElement | null>;
}

function bash(text: string, alternateText?: string) {
  return (
    <div className="flex items-center justify-start gap-1">
      <CopyText className="min-w-[2.25rem]" text={alternateText ?? text} />
      <code className="text-xs text-[lightblue]">{text}</code>
    </div>
  );
}

function CopyAndExplainCode({ code }: { code: string }) {
  const [isOpen, setIsOpen] = useState(false);
  const numCodeLines = code.split("\n").length;

  return (
    <div className="flex items-center justify-end">
      <Dialog open={isOpen} onOpenChange={setIsOpen}>
        <DialogTrigger asChild>
          <Button variant="tertiary" size="sm">
            <div className="flex items-center justify-center gap-2">
              <div>Run Locally</div>
              <PlayIcon />
            </div>
          </Button>
        </DialogTrigger>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Run This Code</DialogTitle>
            <DialogDescription>
              Set up skyvern in your environment and run the code on your own.
            </DialogDescription>
          </DialogHeader>
          <div>
            <div>1. Install skyvern: {bash("pip install skyvern")}</div>
            <div>2. Set up skyvern: {bash("skyvern quickstart")}</div>
            <div>
              3. Copy-paste the code and save it in a file, for example{" "}
              <code>main.py</code>{" "}
              {bash(`copy code [${numCodeLines} line(s)]`, code)}
            </div>
            <div>
              4. Run the code:{" "}
              {bash(
                'skyvern run code --params \'{"param1": "val1", "param2": "val2"}\' main.py',
              )}
            </div>
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => setIsOpen(false)}>
              Ok
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <CopyText text={code} />
    </div>
  );
}

function CopyText({ className, text }: { className?: string; text: string }) {
  const [wasCopied, setWasCopied] = useState(false);

  function handleCopy(code: string) {
    navigator.clipboard.writeText(code);
    setWasCopied(true);
    setTimeout(() => setWasCopied(false), 2000);
  }

  return (
    <Button
      className={className}
      size="icon"
      variant="link"
      onClick={(e) => {
        e.stopPropagation();
        handleCopy(text);
      }}
    >
      {wasCopied ? <CheckIcon /> : <CopyIcon />}
    </Button>
  );
}

function Workspace({
  initialNodes,
  initialEdges,
  initialTitle,
  showBrowser = false,
  workflow,
}: Props) {
  const { blockLabel, workflowPermanentId } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const cacheKeyValueParam = searchParams.get("cache-key-value");
  const [timelineMode, setTimelineMode] = useState("wide");
  const [cacheKeyValueFilter, setCacheKeyValueFilter] = useState<string | null>(
    null,
  );
  const [page, setPage] = useState(1);
  const [nudge, setNudge] = useState(false);
  const { workflowPanelState, setWorkflowPanelState, closeWorkflowPanel } =
    useWorkflowPanelStore();
  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);
  const { getNodes, getEdges } = useReactFlow();
  const saveWorkflow = useWorkflowSave({ status: "published" });

  const { data: workflowRun } = useWorkflowRunQuery();
  const isFinalized = workflowRun ? statusIsFinalized(workflowRun) : false;

  const [openCycleBrowserDialogue, setOpenCycleBrowserDialogue] =
    useState(false);
  const [toDeleteCacheKeyValue, setToDeleteCacheKeyValue] = useState<
    string | null
  >(null);
  const [
    openConfirmCacheKeyValueDeleteDialogue,
    setOpenConfirmCacheKeyValueDeleteDialogue,
  ] = useState(false);
  const [activeDebugSession, setActiveDebugSession] =
    useState<DebugSessionApiResponse | null>(null);
  const [showPowerButton, setShowPowerButton] = useState(true);
  const [reloadKey, setReloadKey] = useState(0);
  const [windowResizeTrigger, setWindowResizeTrigger] = useState(0);
  const [containerResizeTrigger, setContainerResizeTrigger] = useState(0);
  const [isReloading, setIsReloading] = useState(false);
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const [shouldFetchDebugSession, setShouldFetchDebugSession] = useState(false);
  const blockScriptStore = useBlockScriptStore();
  const recordingStore = useRecordingStore();
  const cacheKey = workflow?.cache_key ?? "";

  const [cacheKeyValue, setCacheKeyValue] = useState(
    cacheKey === ""
      ? ""
      : cacheKeyValueParam
        ? cacheKeyValueParam
        : constructCacheKeyValue({ codeKey: cacheKey, workflow }),
  );

  const [showAllCode, setShowAllCode] = useState(false);
  const [leftSideLayoutMode, setLeftSideLayoutMode] = useState<
    "single" | "side-by-side"
  >("single");

  const dom: Dom = {
    splitLeft: useRef<HTMLInputElement>(null),
  };

  // Track all used labels globally (including those in saved branch states)
  // Initialize with labels from initial nodes
  const usedLabelsRef = useRef<Set<string>>(
    new Set(
      initialNodes.filter(isWorkflowBlockNode).map((node) => node.data.label),
    ),
  );

  // Sync usedLabelsRef with current nodes to handle any external changes
  useEffect(() => {
    const currentLabels = nodes
      .filter(isWorkflowBlockNode)
      .map((node) => node.data.label);
    usedLabelsRef.current = new Set(currentLabels);
  }, [nodes]);

  const handleOnSave = async () => {
    const errors = getWorkflowErrors(nodes);
    if (errors.length > 0) {
      toast({
        title: "Encountered error while trying to save workflow:",
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

    workflowChangesStore.setSaidOkToCodeCacheDeletion(false);

    queryClient.invalidateQueries({
      queryKey: ["cache-key-values", workflowPermanentId, cacheKey],
    });

    setCacheKeyValueFilter("");
  };

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setTimelineMode("narrow");
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, []);

  useEffect(() => {
    const currentUrlValue = searchParams.get("cache-key-value");
    const targetValue = cacheKeyValue === "" ? null : cacheKeyValue;

    if (currentUrlValue !== targetValue) {
      setSearchParams(
        (prev) => {
          const newParams = new URLSearchParams(prev);
          if (cacheKeyValue === "") {
            newParams.delete("cache-key-value");
          } else {
            newParams.set("cache-key-value", cacheKeyValue);
          }
          return newParams;
        },
        { replace: true },
      );
    }
  }, [cacheKeyValue, searchParams, setSearchParams]);

  const { data: blockScriptsPublished } = useBlockScriptsQuery({
    cacheKey,
    cacheKeyValue,
    workflowPermanentId,
    status: "published",
  });

  const publishedLabelCount = Object.keys(blockScriptsPublished ?? {}).length;

  const isGeneratingCode =
    publishedLabelCount === 0 && !isFinalized && Boolean(workflowRun);

  const { data: blockScriptsPending } = useBlockScriptsQuery({
    cacheKey,
    cacheKeyValue,
    workflowPermanentId,
    pollIntervalMs: isGeneratingCode ? 3000 : undefined,
    status: "pending",
    workflowRunId: workflowRun?.workflow_run_id,
  });

  const { data: cacheKeyValues, isLoading: cacheKeyValuesLoading } =
    useCacheKeyValuesQuery({
      cacheKey,
      debounceMs: 100,
      filter: cacheKeyValueFilter || undefined,
      page,
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

  const showBreakoutButton =
    activeDebugSession && activeDebugSession.browser_session_id;

  const hasLoopBlock = nodes.some((node) => node.type === "loop");
  const hasHttpBlock = nodes.some((node) => node.type === "http_request");
  const workflowWidth = hasHttpBlock
    ? "39rem"
    : hasLoopBlock
      ? "34.25rem"
      : "34rem";

  /**
   * Open a new tab (not window) with the browser session URL.
   */
  const breakout = () => {
    if (activeDebugSession) {
      const pbsId = activeDebugSession.browser_session_id;
      if (pbsId) {
        window.open(`${location.origin}/browser-session/${pbsId}`, "_blank");
      }
    }
  };

  const cycle = () => {
    setOpenCycleBrowserDialogue(true);
  };

  const reload = () => {
    if (isReloading) {
      return;
    }

    setReloadKey((prev) => prev + 1);
    setIsReloading(true);

    setTimeout(() => {
      setIsReloading(false);
    }, 1000);
  };

  useMountEffect(() => {
    setCollapsed(true);
    workflowChangesStore.setHasChanges(false);

    if (workflowPermanentId) {
      queryClient.removeQueries({
        queryKey: ["debugSession", workflowPermanentId],
      });
      setShouldFetchDebugSession(true);

      queryClient.invalidateQueries({
        queryKey: ["cache-key-values", workflowPermanentId, cacheKey],
      });
    }

    closeWorkflowPanel();
  });

  // Centralized function to manage comparison and panel states
  const clearComparisonViewAndShowFreshIfActive = useCallback(
    (active: boolean) => {
      setWorkflowPanelState({
        active,
        content: "history",
        data: {
          showComparison: false,
          version1: undefined,
          version2: undefined,
        },
      });
    },
    [setWorkflowPanelState],
  );

  // Clear comparison view when switching between browser mode and editor mode
  useEffect(() => {
    if (workflowPanelState.data?.showComparison) {
      clearComparisonViewAndShowFreshIfActive(false);
      setShowAllCode(false);
    }
    // We intentionally omit workflowPanelState.data?.showComparison from deps
    // to avoid clearing comparison immediately when it's set
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showBrowser, clearComparisonViewAndShowFreshIfActive]);

  useMountEffect(() => {
    const closePanelsWhenEscapeIsPressed = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        closeWorkflowPanel();
      }
    };

    document.addEventListener("keydown", closePanelsWhenEscapeIsPressed);

    return () => {
      document.removeEventListener("keydown", closePanelsWhenEscapeIsPressed);
    };
  });

  // Add window resize listener to trigger NoVNC canvas resize
  // invalidate block scripts (so we always fetch latest on mount)
  useEffect(() => {
    const handleResize = () => {
      setWindowResizeTrigger((prev) => prev + 1);
    };

    window.addEventListener("resize", handleResize);

    queryClient.invalidateQueries({
      queryKey: ["block-scripts"],
    });

    return () => window.removeEventListener("resize", handleResize);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (isFinalized) {
      queryClient.invalidateQueries({
        queryKey: ["block-scripts"],
      });

      queryClient.invalidateQueries({
        queryKey: ["cache-key-values"],
      });
    }
  }, [isFinalized, queryClient, workflowRun]);

  useEffect(() => {
    blockScriptStore.setScripts(blockScriptsPublished ?? {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [blockScriptsPublished]);

  const afterCycleBrowser = () => {
    setOpenCycleBrowserDialogue(false);
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

  const deleteCacheKeyValue = useMutation({
    mutationFn: async ({
      workflowPermanentId,
      cacheKeyValue,
    }: {
      workflowPermanentId: string;
      cacheKeyValue: string;
    }) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const encodedCacheKeyValue = encodeURIComponent(cacheKeyValue);
      return client.delete(
        `/scripts/${workflowPermanentId}/value?cache-key-value=${encodedCacheKeyValue}`,
      );
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["cache-key-values", workflowPermanentId, cacheKey],
      });
      setToDeleteCacheKeyValue(null);
      setOpenConfirmCacheKeyValueDeleteDialogue(false);
    },
    onError: (error: AxiosError) => {
      toast({
        variant: "destructive",
        title: "Failed to delete code key value",
        description: error.message,
      });
      setToDeleteCacheKeyValue(null);
      setOpenConfirmCacheKeyValueDeleteDialogue(false);
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

  useEffect(() => {
    const splitLeft = dom.splitLeft.current;

    if (!splitLeft) {
      return;
    }

    const parent = splitLeft.parentElement;

    if (!parent) {
      return;
    }

    const observer = new ResizeObserver(() => {
      setLeftSideLayoutMode(
        parent.offsetWidth < 1100 ? "single" : "side-by-side",
      );
    });

    observer.observe(parent);

    return () => {
      observer.disconnect();
    };
  }, [dom.splitLeft]);

  const doLayout = useCallback(
    (nodes: Array<AppNode>, edges: Array<Edge>) => {
      const layoutedElements = layout(nodes, edges);
      setNodes(layoutedElements.nodes);
      setEdges(layoutedElements.edges);
    },
    [setNodes, setEdges],
  );

  // Listen for conditional branch changes to trigger re-layout
  useEffect(() => {
    const handleBranchChange = () => {
      // Use a small delay to ensure visibility updates have propagated
      setTimeout(() => {
        // Get the latest nodes and edges (including visibility changes)
        const currentNodes = getNodes() as Array<AppNode>;
        const currentEdges = getEdges();

        const layoutedElements = layout(currentNodes, currentEdges);
        setNodes(layoutedElements.nodes);
        setEdges(layoutedElements.edges);
      }, 10); // Small delay to ensure visibility updates complete
    };

    window.addEventListener("conditional-branch-changed", handleBranchChange);
    return () => {
      window.removeEventListener(
        "conditional-branch-changed",
        handleBranchChange,
      );
    };
  }, [getNodes, getEdges, setNodes, setEdges]);

  function addNode({
    nodeType,
    previous,
    next,
    parent,
    connectingEdgeType,
    branch,
  }: AddNodeProps) {
    const newNodes: Array<AppNode> = [];
    const newEdges: Array<Edge> = [];
    const id = nanoid();
    // Use global label tracking instead of just current nodes
    const existingLabels = Array.from(usedLabelsRef.current);
    const newLabel = generateNodeLabel(existingLabels);
    const computedParentId = parent ?? branch?.conditionalNodeId;
    const node = createNode(
      { id, parentId: computedParentId },
      nodeType,
      newLabel,
    );
    // Track the new label
    usedLabelsRef.current.add(newLabel);

    if (branch && "data" in node) {
      node.data = {
        ...node.data,
        conditionalBranchId: branch.branchId,
        conditionalLabel: branch.conditionalLabel,
        conditionalNodeId: branch.conditionalNodeId,
        conditionalMergeLabel: branch.mergeLabel ?? null,
      };
    }
    newNodes.push(node);
    if (previous) {
      const newEdge: Edge = {
        id: nanoid(),
        type: "edgeWithAddButton",
        source: previous,
        target: id,
        style: {
          strokeWidth: 2,
        },
        data: branch
          ? {
              conditionalNodeId: branch.conditionalNodeId,
              conditionalBranchId: branch.branchId,
            }
          : undefined,
      };
      newEdges.push(newEdge);
    }
    if (next) {
      const newEdge: Edge = {
        id: nanoid(),
        type: connectingEdgeType,
        source: id,
        target: next,
        style: {
          strokeWidth: 2,
        },
        data: branch
          ? {
              conditionalNodeId: branch.conditionalNodeId,
              conditionalBranchId: branch.branchId,
            }
          : undefined,
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
            label: "__start_block__",
            showCode: false,
          },
          id,
        ),
      );
      newNodes.push(nodeAdderNode(adderNodeId, id));
      newEdges.push(defaultEdge(startNodeId, adderNodeId));
    }

    if (nodeType === "conditional" && "data" in node) {
      // Conditional blocks need StartNode and NodeAdderNode as children
      const startNodeId = nanoid();
      const adderNodeId = nanoid();
      newNodes.push(
        startNode(
          startNodeId,
          {
            withWorkflowSettings: false,
            editable: true,
            label: "__start_block__",
            showCode: false,
            parentNodeType: "conditional",
          },
          id,
        ),
      );
      newNodes.push(nodeAdderNode(adderNodeId, id));

      // Create an edge for each branch (initially all branches have START → NodeAdder)
      const conditionalData = node.data as ConditionalNodeData;
      const activeBranchId = conditionalData.activeBranchId;
      conditionalData.branches.forEach((branch) => {
        const edge: Edge = {
          id: nanoid(),
          type: "default",
          source: startNodeId,
          target: adderNodeId,
          style: { strokeWidth: 2 },
          data: {
            conditionalNodeId: id,
            conditionalBranchId: branch.id,
          },
          // Only the active branch's edge should be visible initially
          hidden: branch.id !== activeBranchId,
        };
        newEdges.push(edge);
      });
    }

    const editedEdges = previous
      ? edges.filter((edge) => {
          // Don't remove edges from the previous node
          if (edge.source !== previous) {
            return true;
          }
          // If we're in a branch, only remove the edge for this branch
          if (branch) {
            const edgeData = edge.data as
              | { conditionalBranchId?: string }
              | undefined;
            return edgeData?.conditionalBranchId !== branch.branchId;
          }
          // Otherwise remove all edges from previous
          return false;
        })
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

  function openCacheKeyValuesPanel() {
    setWorkflowPanelState({
      active: true,
      content: "cacheKeyValues",
    });
  }

  function toggleCacheKeyValuesPanel() {
    if (
      workflowPanelState.active &&
      workflowPanelState.content === "cacheKeyValues"
    ) {
      closeWorkflowPanel();
    } else {
      openCacheKeyValuesPanel();
    }
  }

  function toggleHistoryPanel() {
    // Capture current state before making changes
    const wasInComparisonMode = workflowPanelState.data?.showComparison;
    const isHistoryPanelOpen =
      workflowPanelState.active && workflowPanelState.content === "history";

    // Always reset code view when toggling history
    setShowAllCode(false);

    if (wasInComparisonMode || isHistoryPanelOpen) {
      // If in comparison mode or history panel is open, close it
      clearComparisonViewAndShowFreshIfActive(false);
    } else {
      // Open history panel fresh
      clearComparisonViewAndShowFreshIfActive(true);
    }
  }

  function toggleCodeView() {
    // Check comparison state BEFORE clearing it
    const wasInComparisonMode = workflowPanelState.data?.showComparison;

    // Always clear comparison state first
    clearComparisonViewAndShowFreshIfActive(false);

    if (wasInComparisonMode) {
      // If we were in comparison mode, exit it and show code
      setShowAllCode(true);
    } else {
      // Normal toggle when not in comparison mode
      setShowAllCode(!showAllCode);
    }
  }

  const orderedBlockLabels = getOrderedBlockLabels(workflow);
  const code = getCode(orderedBlockLabels, blockScriptsPublished).join("");
  const codePending = getCode(orderedBlockLabels, blockScriptsPending).join("");

  const handleCompareVersions = (
    version1: WorkflowVersion,
    version2: WorkflowVersion,
    mode: "visual" | "json" = "visual",
  ) => {
    // Implement visual drawer comparison
    if (mode === "visual") {
      // Keep history panel active but add comparison data
      setWorkflowPanelState({
        active: true,
        content: "history", // Keep history panel active
        data: {
          version1: JSON.parse(JSON.stringify(version1)),
          version2: JSON.parse(JSON.stringify(version2)),
          showComparison: true, // Add flag to show comparison
        },
      });
    }

    // TODO: Implement JSON diff comparison
    if (mode === "json") {
      // This will open a JSON diff view
      console.warn("[Not Implemented] opening JSON diff view...");
      // Future: setJsonDiffOpen(true);
      // Future: setJsonDiffVersions({ version1, version2 });
    }
  };

  const handleSelectState = (selectedVersion: WorkflowVersion) => {
    // Close panels
    setWorkflowPanelState({
      active: false,
      content: "parameters",
      data: {
        showComparison: false,
        version1: undefined,
        version2: undefined,
      },
    });

    // Load the selected version into the main editor
    const settings: WorkflowSettings = {
      proxyLocation:
        selectedVersion.proxy_location || ProxyLocation.Residential,
      webhookCallbackUrl: selectedVersion.webhook_callback_url || "",
      persistBrowserSession: selectedVersion.persist_browser_session,
      model: selectedVersion.model,
      maxScreenshotScrolls: selectedVersion.max_screenshot_scrolls || 3,
      extraHttpHeaders: selectedVersion.extra_http_headers
        ? JSON.stringify(selectedVersion.extra_http_headers)
        : null,
      runWith: selectedVersion.run_with,
      scriptCacheKey: selectedVersion.cache_key,
      aiFallback: selectedVersion.ai_fallback ?? true,
      runSequentially: selectedVersion.run_sequentially ?? false,
      sequentialKey: selectedVersion.sequential_key ?? null,
    };

    const elements = getElements(
      selectedVersion.workflow_definition?.blocks || [],
      settings,
      true, // editable
    );

    // Update the main editor with the selected version
    setNodes(elements.nodes);
    setEdges(elements.edges);
  };

  return (
    <div className="relative h-full w-full">
      {/* cycle browser dialog */}
      <Dialog
        open={openCycleBrowserDialogue}
        onOpenChange={(open) => {
          if (!open && cycleBrowser.isPending) {
            return;
          }
          setOpenCycleBrowserDialogue(open);
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

      {/* confirm code cache deletion dialog */}
      <Dialog
        open={workflowChangesStore.showConfirmCodeCacheDeletion}
        onOpenChange={(open) => {
          !open && workflowChangesStore.setShowConfirmCodeCacheDeletion(false);
          !open && workflowChangesStore.setSaidOkToCodeCacheDeletion(false);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Are you sure?</DialogTitle>
            <DialogDescription>
              Saving will delete cached code, and Skyvern will re-generate it in
              the next run. Proceed?
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose asChild>
              <Button variant="secondary">Cancel</Button>
            </DialogClose>
            <Button
              variant="default"
              onClick={async () => {
                workflowChangesStore.setSaidOkToCodeCacheDeletion(true);
                await handleOnSave();
                workflowChangesStore.setShowConfirmCodeCacheDeletion(false);
              }}
            >
              Yes
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* cache key value delete dialog */}
      <Dialog
        open={openConfirmCacheKeyValueDeleteDialogue}
        onOpenChange={(open) => {
          if (!open && deleteCacheKeyValue.isPending) {
            return;
          }
          setOpenConfirmCacheKeyValueDeleteDialogue(open);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete Generated Code</DialogTitle>
            <DialogDescription>
              <div className="w-full pb-2 pt-4 text-sm text-slate-400">
                {deleteCacheKeyValue.isPending ? (
                  "Deleting generated code..."
                ) : (
                  <div className="flex w-full flex-col gap-2">
                    <div className="w-full">
                      Are you sure you want to delete the generated code for
                      this code key value?
                    </div>
                    <div
                      className="max-w-[29rem] overflow-hidden text-ellipsis whitespace-nowrap text-sm font-bold text-slate-400"
                      title={toDeleteCacheKeyValue ?? undefined}
                    >
                      {toDeleteCacheKeyValue}
                    </div>
                  </div>
                )}
              </div>
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            {!deleteCacheKeyValue.isPending && (
              <DialogClose asChild>
                <Button variant="secondary">Cancel</Button>
              </DialogClose>
            )}
            <Button
              variant="default"
              onClick={() => {
                deleteCacheKeyValue.mutate({
                  workflowPermanentId: workflowPermanentId!,
                  cacheKeyValue: toDeleteCacheKeyValue!,
                });
              }}
              disabled={deleteCacheKeyValue.isPending}
            >
              Yes, Continue{" "}
              {deleteCacheKeyValue.isPending && (
                <ReloadIcon className="ml-2 size-4 animate-spin" />
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* header panel */}
      <div className="absolute left-6 right-6 top-8 z-40 h-20">
        <WorkflowHeader
          cacheKeyValue={cacheKeyValue}
          cacheKeyValues={cacheKeyValues}
          isGeneratingCode={isGeneratingCode}
          isTemplate={workflow?.is_template}
          saving={workflowChangesStore.saveIsPending}
          cacheKeyValuesPanelOpen={
            workflowPanelState.active &&
            workflowPanelState.content === "cacheKeyValues"
          }
          parametersPanelOpen={
            workflowPanelState.active &&
            workflowPanelState.content === "parameters"
          }
          showAllCode={showAllCode}
          onCacheKeyValueAccept={(v) => {
            setCacheKeyValue(v ?? "");
            setCacheKeyValueFilter("");
            closeWorkflowPanel();
          }}
          onCacheKeyValuesBlurred={(v) => {
            setCacheKeyValue(v ?? "");
          }}
          onCacheKeyValuesKeydown={(e) => {
            if (e.key === "Enter") {
              toggleCacheKeyValuesPanel();
              return;
            }

            if (e.key !== "Tab") {
              openCacheKeyValuesPanel();
            }
          }}
          onCacheKeyValuesFilter={(v) => {
            setCacheKeyValueFilter(v);
          }}
          onCacheKeyValuesClick={() => {
            toggleCacheKeyValuesPanel();
          }}
          onParametersClick={() => {
            if (
              workflowPanelState.active &&
              workflowPanelState.content === "parameters"
            ) {
              closeWorkflowPanel();
            } else {
              setWorkflowPanelState({
                active: true,
                content: "parameters",
              });
            }
          }}
          onSave={async () => await handleOnSave()}
          onRun={() => {
            closeWorkflowPanel();
          }}
          onShowAllCodeClick={toggleCodeView}
          onHistory={toggleHistoryPanel}
        />
      </div>

      {/* comparison view (takes precedence over both browser and non-browser modes) */}
      {workflowPanelState.data?.showComparison &&
      workflowPanelState.data?.version1 &&
      workflowPanelState.data?.version2 ? (
        <div className="relative flex h-full w-full overflow-hidden overflow-x-hidden">
          {/* comparison view */}
          <div
            className="absolute left-6 top-[6rem]"
            style={{
              width: "calc(100% - 32rem)",
              height: "calc(100vh - 11rem)",
            }}
          >
            <WorkflowComparisonPanel
              key={`${workflowPanelState.data.version1.workflow_id}v${workflowPanelState.data.version1.version}-${workflowPanelState.data.version2.workflow_id}v${workflowPanelState.data.version2.version}`}
              version1={workflowPanelState.data.version1}
              version2={workflowPanelState.data.version2}
              onSelectState={handleSelectState}
            />
          </div>

          {/* sub panels */}
          {workflowPanelState.active && (
            <div
              className="absolute right-6 top-[8.5rem] z-30"
              style={{
                height:
                  workflowPanelState.content === "nodeLibrary"
                    ? "calc(100vh - 14rem)"
                    : "unset",
              }}
            >
              {workflowPanelState.content === "cacheKeyValues" && (
                <WorkflowCacheKeyValuesPanel
                  cacheKeyValues={cacheKeyValues}
                  pending={cacheKeyValuesLoading}
                  scriptKey={workflow.cache_key ?? "default"}
                  onDelete={(cacheKeyValue) => {
                    setToDeleteCacheKeyValue(cacheKeyValue);
                    setOpenConfirmCacheKeyValueDeleteDialogue(true);
                  }}
                  onPaginate={(page) => {
                    setPage(page);
                  }}
                  onSelect={(cacheKeyValue) => {
                    setCacheKeyValue(cacheKeyValue);
                    setCacheKeyValueFilter("");
                    closeWorkflowPanel();
                  }}
                />
              )}
              {workflowPanelState.content === "parameters" && (
                <div className="z-30">
                  <WorkflowParametersPanel />
                </div>
              )}
              {workflowPanelState.content === "history" && (
                <div className="pointer-events-auto relative right-0 top-[3.5rem] z-30 h-[calc(100vh-14rem)]">
                  <WorkflowHistoryPanel
                    workflowPermanentId={workflowPermanentId!}
                    onCompare={handleCompareVersions}
                  />
                </div>
              )}
              {workflowPanelState.content === "nodeLibrary" && (
                <div className="z-30 h-full w-[25rem]">
                  <WorkflowNodeLibraryPanel
                    onNodeClick={(props) => {
                      addNode(props);
                    }}
                  />
                </div>
              )}
            </div>
          )}
        </div>
      ) : (
        <>
          {/* infinite canvas and sub panels when not in debug mode */}
          {!showBrowser && (
            <div className="relative flex h-full w-full overflow-hidden overflow-x-hidden">
              {/* infinite canvas */}
              <FlowRenderer
                nodes={nodes}
                edges={edges}
                setNodes={setNodes}
                setEdges={setEdges}
                onNodesChange={onNodesChange}
                onEdgesChange={onEdgesChange}
                initialTitle={initialTitle}
                workflow={workflow}
              />

              {/* sub panels */}
              {workflowPanelState.active && (
                <div
                  className="absolute right-6 top-[8.5rem] z-30"
                  style={{
                    height:
                      workflowPanelState.content === "nodeLibrary"
                        ? "calc(100vh - 14rem)"
                        : "unset",
                  }}
                >
                  {workflowPanelState.content === "cacheKeyValues" && (
                    <WorkflowCacheKeyValuesPanel
                      cacheKeyValues={cacheKeyValues}
                      pending={cacheKeyValuesLoading}
                      scriptKey={workflow.cache_key ?? "default"}
                      onDelete={(cacheKeyValue) => {
                        setToDeleteCacheKeyValue(cacheKeyValue);
                        setOpenConfirmCacheKeyValueDeleteDialogue(true);
                      }}
                      onPaginate={(page) => {
                        setPage(page);
                      }}
                      onSelect={(cacheKeyValue) => {
                        setCacheKeyValue(cacheKeyValue);
                        setCacheKeyValueFilter("");
                        closeWorkflowPanel();
                      }}
                    />
                  )}
                  {workflowPanelState.content === "parameters" && (
                    <div className="z-30">
                      <WorkflowParametersPanel />
                    </div>
                  )}
                  {workflowPanelState.content === "history" && (
                    <div className="pointer-events-auto relative right-0 top-[3.5rem] z-30 h-[calc(100vh-14rem)]">
                      <WorkflowHistoryPanel
                        workflowPermanentId={workflowPermanentId!}
                        onCompare={handleCompareVersions}
                      />
                    </div>
                  )}
                  {workflowPanelState.content === "nodeLibrary" && (
                    <div className="z-30 h-full w-[25rem]">
                      <WorkflowNodeLibraryPanel
                        onNodeClick={(props) => {
                          addNode(props);
                        }}
                      />
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </>
      )}

      {/* sub panels (but not node library panel) when in debug mode */}
      {showBrowser &&
        !workflowPanelState.data?.showComparison &&
        workflowPanelState.active &&
        workflowPanelState.content !== "nodeLibrary" && (
          <div className="absolute right-6 top-[8.5rem] z-20">
            {workflowPanelState.content === "cacheKeyValues" && (
              <WorkflowCacheKeyValuesPanel
                cacheKeyValues={cacheKeyValues}
                pending={cacheKeyValuesLoading}
                scriptKey={workflow.cache_key ?? "default"}
                onDelete={(cacheKeyValue) => {
                  setToDeleteCacheKeyValue(cacheKeyValue);
                  setOpenConfirmCacheKeyValueDeleteDialogue(true);
                }}
                onPaginate={(page) => {
                  setPage(page);
                }}
                onSelect={(cacheKeyValue) => {
                  setCacheKeyValue(cacheKeyValue);
                  setCacheKeyValueFilter("");
                  closeWorkflowPanel();
                }}
              />
            )}
            {workflowPanelState.content === "parameters" && (
              <WorkflowParametersPanel />
            )}
            {workflowPanelState.content === "history" && (
              <WorkflowHistoryPanel
                workflowPermanentId={workflowPermanentId!}
                onCompare={handleCompareVersions}
              />
            )}
          </div>
        )}

      {/* code, infinite canvas, browser, timeline, and node library sub panel when in debug mode */}
      {showBrowser && !workflowPanelState.data?.showComparison && (
        <div className="relative flex h-full w-full overflow-hidden overflow-x-hidden">
          <Splitter
            className="splittah"
            classNameLeft="flex items-center justify-center"
            direction="vertical"
            split={{ left: workflowWidth }}
            onResize={() => setContainerResizeTrigger((prev) => prev + 1)}
          >
            {/* code, infinite canvas, and block runs */}
            <div className="relative h-full w-full">
              <div
                className={cn(
                  "skyvern-split-left flex h-full w-[200%] translate-x-[-50%] transition-none duration-300",
                  {
                    "w-[100%] translate-x-0":
                      leftSideLayoutMode === "side-by-side",
                  },
                  {
                    "translate-x-0": showAllCode,
                  },
                )}
                ref={dom.splitLeft}
              >
                {/* code */}
                <div
                  className={cn("h-full w-[50%]", {
                    "w-[0%]":
                      leftSideLayoutMode === "side-by-side" && !showAllCode,
                  })}
                >
                  <div className="relative mt-[8.5rem] w-full p-6 pr-5 pt-0">
                    <div className="absolute right-[2rem] top-[0.75rem] z-20">
                      <CopyAndExplainCode code={code} />
                    </div>
                    <CodeEditor
                      className={cn("w-full overflow-y-scroll", {
                        "animate-pulse": isGeneratingCode,
                      })}
                      language="python"
                      value={isGeneratingCode ? codePending : code}
                      lineWrap={false}
                      readOnly
                      fontSize={10}
                    />
                  </div>
                </div>
                {/* infinite canvas */}
                <div
                  className={cn("h-full w-[50%]", {
                    "w-[100%]":
                      leftSideLayoutMode === "side-by-side" && !showAllCode,
                  })}
                >
                  <FlowRenderer
                    hideBackground={true}
                    hideControls={true}
                    nodes={nodes}
                    edges={edges}
                    setNodes={setNodes}
                    setEdges={setEdges}
                    onNodesChange={onNodesChange}
                    onEdgesChange={onEdgesChange}
                    initialTitle={initialTitle}
                    workflow={workflow}
                    onContainerResize={containerResizeTrigger}
                  />
                </div>
              </div>
              {/* block runs history for current debug session id*/}
              <div className="absolute bottom-[0.5rem] left-[0.75rem] flex w-full items-start justify-center">
                <DebuggerBlockRuns />
              </div>
            </div>

            <div className="skyvern-split-right relative flex h-full items-end justify-center bg-[#020617] p-4 pl-6">
              {/* node library sub panel */}
              {workflowPanelState.active &&
                workflowPanelState.content === "nodeLibrary" && (
                  <div
                    className="absolute left-6 top-[8.5rem] z-30"
                    style={{
                      height: "calc(100vh - 14rem)",
                    }}
                  >
                    <div className="z-30 h-full w-[25rem]">
                      <WorkflowNodeLibraryPanel
                        onNodeClick={(props) => {
                          addNode(props);
                        }}
                      />
                    </div>
                  </div>
                )}

              {/* browser & timeline */}
              <div className="flex h-[calc(100%_-_8rem)] w-full gap-6">
                {/* VNC browser */}
                {(!activeDebugSession ||
                  activeDebugSession.vnc_streaming_supported) && (
                  <div className="skyvern-vnc-browser flex h-full w-[calc(100%_-_6rem)] flex-1 flex-col items-center justify-center">
                    <div key={reloadKey} className="w-full flex-1">
                      <BrowserStream
                        exfiltrate={recordingStore.isRecording}
                        interactive={true}
                        browserSessionId={
                          activeDebugSession?.browser_session_id
                        }
                        showControlButtons={true}
                        resizeTrigger={windowResizeTrigger}
                      />
                    </div>
                    <footer className="flex h-[2rem] w-full items-center justify-start gap-4">
                      <div className="flex items-center gap-2">
                        <GlobeIcon /> Live Browser
                      </div>
                      {showBreakoutButton && (
                        <BreakoutButton onClick={() => breakout()} />
                      )}
                      <div
                        className={cn("ml-auto flex items-center gap-2", {
                          "mr-16": !blockLabel,
                        })}
                      >
                        {!recordingStore.isRecording && showPowerButton && (
                          <PowerButton onClick={() => cycle()} />
                        )}
                        {!recordingStore.isRecording && (
                          <ReloadButton
                            isReloading={isReloading}
                            onClick={() => reload()}
                          />
                        )}
                      </div>
                    </footer>
                  </div>
                )}

                {/* Screenshot browser} */}
                {activeDebugSession &&
                  !activeDebugSession.vnc_streaming_supported && (
                    <div className="skyvern-screenshot-browser flex h-full w-[calc(100%_-_6rem)] flex-1 flex-col items-center justify-center">
                      <div className="aspect-video w-full">
                        <WorkflowRunStream alwaysShowStream={true} />
                      </div>
                    </div>
                  )}

                {/* timeline */}
                <div
                  className={cn(
                    "z-[15] h-full w-[5rem] overflow-visible",
                    {
                      "skyvern-animate-nudge": nudge,
                    },
                    {
                      "pointer-events-none hidden w-[0px] overflow-hidden":
                        !blockLabel,
                    },
                  )}
                  onMouseEnter={() => {
                    if (timelineMode === "narrow") {
                      return;
                    }

                    setNudge(true);
                  }}
                  onMouseLeave={() => {
                    if (timelineMode === "narrow") {
                      return;
                    }

                    setNudge(false);
                  }}
                >
                  <div
                    className={cn(
                      "group relative h-full w-[25rem] translate-x-[-20.5rem] bg-[#020617] transition-all",
                      {
                        "translate-x-[0rem]": timelineMode === "narrow",
                        group: timelineMode === "narrow",
                      },
                    )}
                    onClick={() => {
                      if (timelineMode === "narrow") {
                        setTimelineMode("wide");
                      }
                    }}
                  >
                    {/* timeline wide */}
                    <div
                      className={cn(
                        "pointer-events-none absolute left-[0.5rem] right-0 top-0 flex h-full w-[400px] flex-col items-end justify-end opacity-0 transition-all duration-1000",
                        {
                          "opacity-100": timelineMode === "wide",
                        },
                      )}
                    >
                      <div
                        className={cn(
                          "pointer-events-none relative flex h-full w-full flex-col items-start overflow-hidden bg-[#020617]",
                          { "pointer-events-auto": timelineMode === "wide" },
                        )}
                      >
                        <DebuggerRun />
                      </div>
                    </div>

                    {/* divider */}
                    <div className="vertical-line-gradient absolute left-0 top-0 h-full w-[2px]"></div>

                    {/* slide nudge ghost */}
                    <div
                      className={cn(
                        "slide-nudge-ghost vertical-line-gradient absolute left-0 top-0 h-full w-[2rem] opacity-0 transition-opacity",
                        {
                          "skyvern-animate-ghost": nudge,
                        },
                      )}
                    />

                    {/* slide indicator */}
                    <div
                      className="absolute left-0 top-0 z-20 flex h-full items-center justify-center p-1 opacity-30 transition-opacity hover:opacity-100 group-hover:opacity-100"
                      onClick={(e) => {
                        e.stopPropagation();
                        setTimelineMode(
                          timelineMode === "wide" ? "narrow" : "wide",
                        );
                      }}
                    >
                      {timelineMode === "narrow" && <ChevronLeftIcon />}
                      {timelineMode === "wide" && <ChevronRightIcon />}
                    </div>

                    {/* timeline narrow */}
                    <div
                      className={cn(
                        "delay-[300ms] pointer-events-none absolute left-0 top-0 h-full w-[6rem] rounded-l-lg opacity-0 transition-all duration-1000",
                        {
                          "pointer-events-auto opacity-100":
                            timelineMode === "narrow",
                        },
                      )}
                    >
                      <DebuggerRunMinimal />
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </Splitter>
        </div>
      )}
    </div>
  );
}

export { CopyText, CopyAndExplainCode, Workspace };
