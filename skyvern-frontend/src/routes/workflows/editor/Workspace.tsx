import { AxiosError } from "axios";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  MutableRefObject,
} from "react";
import { nanoid } from "nanoid";
import { stringify as convertToYAML } from "yaml";
import {
  CheckIcon,
  ChevronRightIcon,
  ChevronLeftIcon,
  CopyIcon,
  GlobeIcon,
  PlayIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import {
  useLocation,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import {
  useEdgesState,
  useNodesState,
  useReactFlow,
  Edge,
} from "@xyflow/react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { usePostHog } from "posthog-js/react";

import { getClient } from "@/api/AxiosClient";
import { DebugSessionApiResponse, ProxyLocation } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useMountEffect } from "@/hooks/useMountEffect";
import { useBrowserSessionRateLimit } from "../hooks/useBrowserSessionRateLimit";
import { useDebugSessionQuery } from "../hooks/useDebugSessionQuery";
import { useBlockScriptsQuery } from "@/routes/workflows/hooks/useBlockScriptsQuery";
import { BrowserSessionStream } from "@/routes/browserSessions/BrowserSessionStream";
import { browserStreamingMode } from "@/util/env";
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
import { DeleteConfirmationDialog } from "@/components/DeleteConfirmationDialog";
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
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { getCode, getOrderedBlockLabels } from "@/routes/workflows/utils";
import { DebuggerBlockRuns } from "@/routes/workflows/debugger/DebuggerBlockRuns";
import { copyText } from "@/util/copyText";
import { isMacPlatform } from "@/util/platform";
import { cn } from "@/util/utils";

import { FlowRenderer, type FlowRendererProps } from "./FlowRenderer";
import { useWorkflowHistory } from "./hooks/useWorkflowHistory";
import { AppNode, isWorkflowBlockNode, WorkflowBlockNode } from "./nodes";
import { ConditionalNodeData } from "./nodes/ConditionalNode/types";
import { WorkflowNodeLibraryPanel } from "./panels/WorkflowNodeLibraryPanel";
import { WorkflowParametersPanel } from "./panels/WorkflowParametersPanel";
import { WorkflowCacheKeyValuesPanel } from "./panels/WorkflowCacheKeyValuesPanel";
import {
  WorkflowComparisonPanel,
  type CopilotReviewStatus,
} from "./panels/WorkflowComparisonPanel";
import {
  getWorkflowErrors,
  getElements,
  getAffectedBlocks,
  getOutputParameterKey,
  nodeAdderNode,
  createNode,
  defaultEdge,
  generateNodeLabel,
  layout,
  startNode,
} from "./workflowEditorUtils";
import { WorkflowHeader } from "./WorkflowHeader";
import { WorkflowHistoryPanel } from "./panels/WorkflowHistoryPanel";
import { WorkflowSchedulePanel } from "./panels/schedulePanel/WorkflowSchedulePanel";
import { WorkflowVersion } from "../hooks/useWorkflowVersionsQuery";
import { WorkflowSettings } from "../types/workflowTypes";

import { constructCacheKeyValue, getInitialParameters } from "./utils";
import { WorkflowCopilotChat } from "../copilot/WorkflowCopilotChat";
import { WorkflowCopilotButton } from "../copilot/WorkflowCopilotButton";

import type { WorkflowYAMLConversionResponse } from "../copilot/workflowCopilotTypes";
import "./workspace-styles.css";

const Constants = {
  NewBrowserCooldown: 30000,
} as const;

// How long to poll before recording one rate-limit attempt (60s)
const POLL_ATTEMPT_THRESHOLD_MS = 60_000;

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

  async function handleCopy(code: string) {
    await copyText(code);
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
  const location = useLocation();
  const navigate = useNavigate();
  const locationState = location.state as { copilotMessage?: unknown } | null;
  const initialCopilotMessage =
    typeof locationState?.copilotMessage === "string"
      ? locationState.copilotMessage
      : null;
  const handleInitialCopilotMessageConsumed = useCallback(() => {
    if (!initialCopilotMessage) return;
    navigate(location.pathname + location.search, {
      replace: true,
      state: null,
    });
  }, [initialCopilotMessage, location.pathname, location.search, navigate]);
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
  const postHog = usePostHog();
  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);
  const {
    undo: undoWorkflowEdit,
    redo: redoWorkflowEdit,
    canUndo: canUndoWorkflowEdit,
    canRedo: canRedoWorkflowEdit,
  } = useWorkflowHistory({ nodes, edges, setNodes, setEdges });
  const { getNodes, getEdges } = useReactFlow();
  const saveWorkflow = useWorkflowSave({ status: "published" });
  const { data: workflowRun } = useWorkflowRunQuery();
  const isFinalized = workflowRun ? statusIsFinalized(workflowRun) : false;

  const [openCycleBrowserDialogue, setOpenCycleBrowserDialogue] =
    useState(false);
  const [isCopilotOpen, setIsCopilotOpen] = useState(
    () => !!initialCopilotMessage || !initialNodes.some(isWorkflowBlockNode),
  );
  const [copilotMessageCount, setCopilotMessageCount] = useState(0);
  const copilotButtonRef = useRef<HTMLButtonElement>(null);
  const [activeDebugSession, setActiveDebugSession] =
    useState<DebugSessionApiResponse | null>(null);
  const [readyBrowserSessionId, setReadyBrowserSessionId] = useState<
    string | null
  >(null);
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

  // Block delete confirmation dialog state
  const [deleteBlockDialogState, setDeleteBlockDialogState] = useState<{
    open: boolean;
    nodeId: string | null;
    nodeLabel: string | null;
  }>({
    open: false,
    nodeId: null,
    nodeLabel: null,
  });
  // Use a ref for the callback to avoid storing functions in state
  const deleteConfirmCallbackRef = useRef<(() => void) | null>(null);

  const affectedBlocksForDelete = useMemo(() => {
    if (!deleteBlockDialogState.nodeLabel) {
      return [];
    }
    const outputKey = getOutputParameterKey(deleteBlockDialogState.nodeLabel);
    return getAffectedBlocks(nodes, outputKey);
  }, [nodes, deleteBlockDialogState.nodeLabel]);

  const handleRequestDeleteNode = useCallback(
    (nodeId: string, nodeLabel: string, confirmCallback: () => void) => {
      const outputKey = getOutputParameterKey(nodeLabel);
      const affected = getAffectedBlocks(nodes, outputKey);
      if (affected.length === 0) {
        confirmCallback();
        return;
      }
      deleteConfirmCallbackRef.current = confirmCallback;
      setDeleteBlockDialogState({
        open: true,
        nodeId,
        nodeLabel,
      });
    },
    [nodes],
  );

  const [cacheKeyValue, setCacheKeyValue] = useState(
    cacheKey === ""
      ? ""
      : cacheKeyValueParam
        ? cacheKeyValueParam
        : constructCacheKeyValue({ codeKey: cacheKey, workflow }),
  );

  // Track whether the cache-key-value was explicitly provided in URL or user-selected.
  // When false, the auto-computed value should NOT appear in the URL.
  const cacheKeyValueIsExplicitRef = useRef(!!cacheKeyValueParam);

  // Helper that marks the cache key value as explicitly user-selected before updating state.
  // Centralizes the ref+state pair so future handlers can't forget to set the ref.
  const setExplicitCacheKeyValue = useCallback((v: string) => {
    cacheKeyValueIsExplicitRef.current = true;
    setCacheKeyValue(v);
  }, []);

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

  // Undo/redo keyboard shortcuts. Skip when the user is typing inside an
  // editable element so the browser's native per-input undo keeps working.
  const isRecording = recordingStore.isRecording;
  // macOS users expect Cmd+Y to be browser "History Forward" (some apps
  // bind it to "Redo Typing"), so we only honour Ctrl+Y on non-Mac.
  // Memoized so the platform sniff runs exactly once per mount.
  const isMac = useMemo(() => isMacPlatform(), []);
  useEffect(() => {
    const isEditableTarget = (target: EventTarget | null): boolean => {
      if (!(target instanceof HTMLElement)) return false;
      const tag = target.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
        return true;
      }
      if (target.isContentEditable) return true;
      // Monaco wraps its editor surface in a div with role="textbox"; let
      // it keep native undo as well.
      if (target.getAttribute("role") === "textbox") return true;
      return false;
    };

    const handleKeyDown = (event: KeyboardEvent) => {
      // Recording owns the editor - don't let hotkeys mutate state behind
      // the disabled toolbar buttons.
      if (isRecording) return;
      // IME composition (CJK, accents) fires keydown events we must not
      // intercept - those belong to the composition flow.
      if (event.isComposing) return;
      const mod = event.metaKey || event.ctrlKey;
      if (!mod) return;
      // Match the typed character via event.key rather than event.code.
      // On QWERTZ / Dvorak / AZERTY the labeled Z key is at a different
      // physical position than US QWERTY, so matching event.code would
      // either miss the user's Cmd+Z entirely or fire undo when they
      // press a different key. event.key honors the keycap label.
      const key = event.key.toLowerCase();
      const isZ = key === "z";
      const isY = key === "y";
      if (!isZ && !isY) return;
      if (isY && isMac) return;
      if (isEditableTarget(event.target)) return;

      if (isZ && !event.shiftKey) {
        event.preventDefault();
        undoWorkflowEdit();
      } else if ((isZ && event.shiftKey) || isY) {
        // Cmd/Ctrl+Shift+Z is the universal redo; Ctrl+Y is the
        // Windows/Linux alternate redo binding (not accepted on Mac).
        event.preventDefault();
        redoWorkflowEdit();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [undoWorkflowEdit, redoWorkflowEdit, isRecording, isMac]);

  useEffect(() => {
    const currentUrlValue = searchParams.get("cache-key-value");

    if (!cacheKeyValueIsExplicitRef.current) {
      // Auto-computed value: remove param from URL if present
      if (currentUrlValue !== null) {
        setSearchParams(
          (prev) => {
            const newParams = new URLSearchParams(prev);
            newParams.delete("cache-key-value");
            return newParams;
          },
          { replace: true },
        );
      }
      return;
    }

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

  const publishedLabelCount = Object.keys(
    blockScriptsPublished?.blocks ?? {},
  ).length;
  const hasPublishedScript =
    publishedLabelCount > 0 || Boolean(blockScriptsPublished?.main_script);

  const isGeneratingCode =
    !hasPublishedScript && !isFinalized && Boolean(workflowRun);

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

  const { isRateLimited, recordAttempt, resetOnSuccess } =
    useBrowserSessionRateLimit(workflowPermanentId);

  const { data: debugSession } = useDebugSessionQuery({
    workflowPermanentId,
    enabled: shouldFetchDebugSession && !!workflowPermanentId,
    isRateLimited,
  });

  const setCollapsed = useSidebarStore((state) => {
    return state.setCollapsed;
  });

  const workflowChangesStore = useWorkflowHasChangesStore();

  const showBreakoutButton =
    activeDebugSession && activeDebugSession.browser_session_id;
  const liveBrowserSessionId = activeDebugSession?.browser_session_id ?? null;
  const copilotRequiresLiveBrowser =
    showBrowser && shouldFetchDebugSession && !isRateLimited;
  // readyBrowserSessionId is keyed to the browser session id rather than a
  // bare boolean: when activeDebugSession's id changes, stale ready state
  // from the previous session cannot leak into the next render.
  const copilotLiveBrowserReady = Boolean(
    readyBrowserSessionId && readyBrowserSessionId === liveBrowserSessionId,
  );

  const handleLiveBrowserReadyChange = useCallback(
    (ready: boolean, sessionId: string | null) => {
      setReadyBrowserSessionId(ready ? sessionId : null);
    },
    [],
  );

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
        window.open(
          `${window.location.origin}/browser-session/${pbsId}`,
          "_blank",
        );
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
    blockScriptStore.setScripts(blockScriptsPublished?.blocks ?? {});
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
      resetOnSuccess();

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
      recordAttempt();

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
    },
    onError: (error: AxiosError) => {
      toast({
        variant: "destructive",
        title: "Failed to delete code key value",
        description: error.message,
      });
    },
  });

  const intervalRef = useRef<NodeJS.Timeout | null>(null);
  const powerButtonTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const pollingStartRef = useRef<number | null>(null);

  // Polling loop: invalidate the debug-session query on an interval while
  // we're waiting for a browser session. Records a rate-limit attempt after
  // sustained polling without success.
  useEffect(() => {
    if (
      (!debugSession || !debugSession.browser_session_id) &&
      shouldFetchDebugSession &&
      workflowPermanentId &&
      !isRateLimited
    ) {
      if (!pollingStartRef.current) {
        pollingStartRef.current = Date.now();
      }

      intervalRef.current = setInterval(() => {
        // After sustained polling without success, record one attempt
        if (
          pollingStartRef.current &&
          Date.now() - pollingStartRef.current >= POLL_ATTEMPT_THRESHOLD_MS
        ) {
          recordAttempt();
          pollingStartRef.current = Date.now();
        }

        queryClient.invalidateQueries({
          queryKey: ["debugSession", workflowPermanentId],
        });
      }, 5000);
    } else {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
      // Reset polling timer so it doesn't carry a stale timestamp into the
      // next polling cycle (e.g. after a rate-limit window expires).
      pollingStartRef.current = null;

      if (debugSession) {
        setActiveDebugSession(debugSession);
      }
    }

    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
      }
    };
  }, [
    debugSession,
    shouldFetchDebugSession,
    workflowPermanentId,
    queryClient,
    isRateLimited,
    recordAttempt,
  ]);

  // Reset rate-limit state when a browser session is successfully acquired.
  // Separated from the polling effect to avoid a circular dependency where
  // resetOnSuccess is both called inside and listed as a dependency.
  useEffect(() => {
    if (debugSession?.browser_session_id) {
      resetOnSuccess();
    }
  }, [debugSession?.browser_session_id, resetOnSuccess]);

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
      const layoutedElements = layout(nodes, edges, blockLabel);
      setNodes(layoutedElements.nodes);
      setEdges(layoutedElements.edges);
    },
    [setNodes, setEdges, blockLabel],
  );

  // Listen for conditional branch changes to trigger re-layout
  useEffect(() => {
    const handleBranchChange = () => {
      // Use a small delay to ensure visibility updates have propagated
      setTimeout(() => {
        // Get the latest nodes and edges (including visibility changes)
        const currentNodes = getNodes() as Array<AppNode>;
        const currentEdges = getEdges();

        const layoutedElements = layout(currentNodes, currentEdges, blockLabel);
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
  }, [getNodes, getEdges, setNodes, setEdges, blockLabel]);

  // Re-layout when a loop node's header height changes (e.g., data schema toggled)
  useEffect(() => {
    const handleLoopHeaderResized = () => {
      setTimeout(() => {
        const currentNodes = getNodes() as Array<AppNode>;
        const currentEdges = getEdges();

        const layoutedElements = layout(currentNodes, currentEdges, blockLabel);
        setNodes(layoutedElements.nodes);
        setEdges(layoutedElements.edges);
      }, 10);
    };

    window.addEventListener("loop-header-resized", handleLoopHeaderResized);
    return () => {
      window.removeEventListener(
        "loop-header-resized",
        handleLoopHeaderResized,
      );
    };
  }, [getNodes, getEdges, setNodes, setEdges, blockLabel]);

  // Re-layout when a conditional node's header height changes (e.g., expression textarea resized)
  useEffect(() => {
    const handleConditionalHeaderResized = () => {
      setTimeout(() => {
        const currentNodes = getNodes() as Array<AppNode>;
        const currentEdges = getEdges();

        const layoutedElements = layout(currentNodes, currentEdges, blockLabel);
        setNodes(layoutedElements.nodes);
        setEdges(layoutedElements.edges);
      }, 10);
    };

    window.addEventListener(
      "conditional-header-resized",
      handleConditionalHeaderResized,
    );
    return () => {
      window.removeEventListener(
        "conditional-header-resized",
        handleConditionalHeaderResized,
      );
    };
  }, [getNodes, getEdges, setNodes, setEdges, blockLabel]);

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
    postHog.capture("builder.block.added", {
      org_id: workflow.organization_id,
      block_type: nodeType,
      position: previousNodeIndex + 1,
    });
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
  const code = getCode(orderedBlockLabels, blockScriptsPublished?.blocks).join(
    "",
  );
  const codePending = getCode(
    orderedBlockLabels,
    blockScriptsPending?.blocks,
  ).join("");

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

  const applyWorkflowUpdate = (
    workflowData: WorkflowVersion,
    options?: { persisted?: boolean },
  ) => {
    const settings: WorkflowSettings = {
      proxyLocation: workflowData.proxy_location ?? ProxyLocation.Residential,
      webhookCallbackUrl: workflowData.webhook_callback_url || "",
      persistBrowserSession: workflowData.persist_browser_session ?? false,
      model: workflowData.model ?? null,
      maxScreenshotScrolls: workflowData.max_screenshot_scrolls || 3,
      extraHttpHeaders: workflowData.extra_http_headers
        ? JSON.stringify(workflowData.extra_http_headers)
        : null,
      runWith: workflowData.run_with ?? "agent",
      codeVersion: workflowData.code_version ?? null,
      scriptCacheKey: workflowData.cache_key ?? null,
      aiFallback: workflowData.ai_fallback ?? true,
      runSequentially: workflowData.run_sequentially ?? false,
      sequentialKey: workflowData.sequential_key ?? null,
      finallyBlockLabel:
        workflowData.workflow_definition?.finally_block_label ?? null,
      workflowSystemPrompt:
        workflowData.workflow_definition?.workflow_system_prompt ?? null,
    };

    const elements = getElements(
      workflowData.workflow_definition.blocks,
      settings,
      true,
    );

    setNodes(elements.nodes);
    setEdges(elements.edges);

    const initialParameters = getInitialParameters(workflowData);
    useWorkflowParametersStore.getState().setParameters(initialParameters);

    if (options?.persisted) {
      // Atomic accept: server wrote a new version; treat as clean baseline and refresh cached workflow.
      workflowChangesStore.setHasChanges(false);
      if (workflowPermanentId) {
        queryClient.invalidateQueries({
          queryKey: ["workflow", workflowPermanentId],
        });
      }
    } else {
      workflowChangesStore.setHasChanges(true);
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
        selectedVersion.proxy_location ?? ProxyLocation.Residential,
      webhookCallbackUrl: selectedVersion.webhook_callback_url || "",
      persistBrowserSession: selectedVersion.persist_browser_session,
      model: selectedVersion.model,
      maxScreenshotScrolls: selectedVersion.max_screenshot_scrolls || 3,
      extraHttpHeaders: selectedVersion.extra_http_headers
        ? JSON.stringify(selectedVersion.extra_http_headers)
        : null,
      runWith: selectedVersion.run_with ?? "agent",
      codeVersion: selectedVersion.code_version ?? null,
      scriptCacheKey: selectedVersion.cache_key,
      aiFallback: selectedVersion.ai_fallback ?? true,
      runSequentially: selectedVersion.run_sequentially ?? false,
      sequentialKey: selectedVersion.sequential_key ?? null,
      finallyBlockLabel:
        selectedVersion.workflow_definition?.finally_block_label ?? null,
      workflowSystemPrompt:
        selectedVersion.workflow_definition?.workflow_system_prompt ?? null,
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

      {/* header panel */}
      <div className="absolute left-6 right-6 top-8 z-40 h-20">
        <WorkflowHeader
          cacheKeyValue={cacheKeyValue}
          cacheKeyValues={cacheKeyValues}
          canUndo={canUndoWorkflowEdit}
          canRedo={canRedoWorkflowEdit}
          onUndo={undoWorkflowEdit}
          onRedo={redoWorkflowEdit}
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
          schedulesPanelOpen={
            workflowPanelState.active &&
            workflowPanelState.content === "schedules"
          }
          showAllCode={showAllCode}
          onCacheKeyValueAccept={(v) => {
            setExplicitCacheKeyValue(v ?? "");
            setCacheKeyValueFilter("");
            closeWorkflowPanel();
          }}
          onCacheKeyValuesBlurred={(v) => {
            setExplicitCacheKeyValue(v ?? "");
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
          onScheduleClick={() => {
            if (
              workflowPanelState.active &&
              workflowPanelState.content === "schedules"
            ) {
              closeWorkflowPanel();
            } else {
              setWorkflowPanelState({
                active: true,
                content: "schedules",
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
              width: workflowPanelState.active
                ? "calc(100% - 32rem)"
                : "calc(100% - 3rem)",
              height: "calc(100vh - 11rem)",
            }}
          >
            <WorkflowComparisonPanel
              key={`${workflowPanelState.data.version1.workflow_id}v${workflowPanelState.data.version1.version}-${workflowPanelState.data.version2.workflow_id}v${workflowPanelState.data.version2.version}`}
              version1={workflowPanelState.data.version1}
              version2={workflowPanelState.data.version2}
              onSelectState={handleSelectState}
              mode={workflowPanelState.data.mode}
              onCopilotReviewClose={
                workflowPanelState.data.onCopilotReviewClose
              }
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
                    deleteCacheKeyValue.mutate({
                      workflowPermanentId: workflowPermanentId!,
                      cacheKeyValue,
                    });
                  }}
                  onPaginate={(page) => {
                    setPage(page);
                  }}
                  onSelect={(cacheKeyValue) => {
                    setExplicitCacheKeyValue(cacheKeyValue);
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
              {workflowPanelState.content === "schedules" && (
                <div className="z-30">
                  <WorkflowSchedulePanel />
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
                onRequestDeleteNode={handleRequestDeleteNode}
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
                        deleteCacheKeyValue.mutate({
                          workflowPermanentId: workflowPermanentId!,
                          cacheKeyValue,
                        });
                      }}
                      onPaginate={(page) => {
                        setPage(page);
                      }}
                      onSelect={(cacheKeyValue) => {
                        setExplicitCacheKeyValue(cacheKeyValue);
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
                  {workflowPanelState.content === "schedules" && (
                    <div className="z-30">
                      <WorkflowSchedulePanel />
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
                  deleteCacheKeyValue.mutate({
                    workflowPermanentId: workflowPermanentId!,
                    cacheKeyValue,
                  });
                }}
                onPaginate={(page) => {
                  setPage(page);
                }}
                onSelect={(cacheKeyValue) => {
                  setExplicitCacheKeyValue(cacheKeyValue);
                  setCacheKeyValueFilter("");
                  closeWorkflowPanel();
                }}
              />
            )}
            {workflowPanelState.content === "parameters" && (
              <WorkflowParametersPanel />
            )}
            {workflowPanelState.content === "schedules" && (
              <WorkflowSchedulePanel />
            )}
            {workflowPanelState.content === "history" && (
              <div className="h-[calc(100vh-14rem)]">
                <WorkflowHistoryPanel
                  workflowPermanentId={workflowPermanentId!}
                  onCompare={handleCompareVersions}
                />
              </div>
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
                    onRequestDeleteNode={handleRequestDeleteNode}
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
                    {isRateLimited ? (
                      <div
                        data-testid="browser-rate-limit-message"
                        className="flex w-full flex-1 items-center justify-center"
                      >
                        <div className="flex max-w-md flex-col items-center justify-center gap-4 rounded-md border border-slate-700 bg-slate-900 p-8 text-center">
                          <p className="text-sm text-slate-300">
                            Failed to load a browser. We have a high demand for
                            browsers right now. The browser will become
                            available again automatically in ~30 minutes. If the
                            issue persists, please contact support.
                          </p>
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => {
                              resetOnSuccess();
                              queryClient.invalidateQueries({
                                queryKey: ["debugSession", workflowPermanentId],
                              });
                            }}
                          >
                            Try again
                          </Button>
                        </div>
                      </div>
                    ) : (
                      <div key={reloadKey} className="w-full flex-1">
                        <BrowserStream
                          exfiltrate={recordingStore.isRecording}
                          interactive={true}
                          browserSessionId={
                            activeDebugSession?.browser_session_id
                          }
                          showControlButtons={true}
                          resizeTrigger={windowResizeTrigger}
                          isExecuting={!!workflowRun && !isFinalized}
                          onReadyChange={handleLiveBrowserReadyChange}
                        />
                      </div>
                    )}
                    <footer className="flex h-[2rem] w-full items-center justify-start gap-4">
                      <WorkflowCopilotButton
                        ref={copilotButtonRef}
                        messageCount={copilotMessageCount}
                        onClick={() => setIsCopilotOpen((prev) => !prev)}
                      />
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
                        {!recordingStore.isRecording &&
                          showPowerButton &&
                          !isRateLimited && (
                            <PowerButton onClick={() => cycle()} />
                          )}
                        {!recordingStore.isRecording && !isRateLimited && (
                          <ReloadButton
                            isReloading={isReloading}
                            onClick={() => reload()}
                          />
                        )}
                      </div>
                    </footer>
                  </div>
                )}

                {/* CDP screencast: only in local mode when VNC is not supported */}
                {activeDebugSession &&
                  !activeDebugSession.vnc_streaming_supported &&
                  browserStreamingMode === "cdp" && (
                    <div className="skyvern-screenshot-browser flex h-full w-[calc(100%_-_6rem)] flex-1 flex-col items-center justify-center">
                      <div
                        key={reloadKey}
                        className="flex w-full flex-1 items-center justify-center"
                      >
                        <BrowserSessionStream
                          browserSessionId={
                            activeDebugSession.browser_session_id
                          }
                          interactive={true}
                          showControlButtons={true}
                          onReadyChange={handleLiveBrowserReadyChange}
                        />
                      </div>
                      <footer className="flex h-[2rem] w-full items-center justify-start gap-4">
                        <WorkflowCopilotButton
                          ref={copilotButtonRef}
                          messageCount={copilotMessageCount}
                          onClick={() => setIsCopilotOpen((prev) => !prev)}
                        />
                        <div className="flex items-center gap-2">
                          <GlobeIcon /> Live Browser
                        </div>
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

                {/* Fallback: non-local without VNC (edge case) */}
                {activeDebugSession &&
                  !activeDebugSession.vnc_streaming_supported &&
                  browserStreamingMode !== "cdp" && (
                    <div className="flex h-full w-[calc(100%_-_6rem)] flex-1 items-center justify-center text-muted-foreground">
                      Browser streaming unavailable
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

      <WorkflowCopilotChat
        isOpen={isCopilotOpen}
        onClose={() => setIsCopilotOpen(false)}
        onMessageCountChange={setCopilotMessageCount}
        buttonRef={copilotButtonRef}
        liveBrowserSessionId={
          copilotLiveBrowserReady ? liveBrowserSessionId : null
        }
        requiresLiveBrowser={copilotRequiresLiveBrowser}
        isLiveBrowserReady={copilotLiveBrowserReady}
        initialMessage={initialCopilotMessage ?? undefined}
        onInitialMessageConsumed={handleInitialCopilotMessageConsumed}
        onReviewWorkflow={async (pendingWorkflow, clearPending) => {
          const saveData = workflowChangesStore.getSaveData?.();
          if (!saveData) return;

          try {
            // Create YAML from current workflow definition only
            const workflowDefinitionYaml = convertToYAML({
              version: saveData.workflowDefinitionVersion,
              parameters: saveData.parameters,
              blocks: saveData.blocks,
              finally_block_label:
                saveData.settings.finallyBlockLabel ?? undefined,
              workflow_system_prompt:
                saveData.settings.workflowSystemPrompt ?? undefined,
            });

            // Convert current workflow definition YAML to blocks
            const client = await getClient(credentialGetter, "sans-api-v1");

            const currentConversionResponse =
              await client.post<WorkflowYAMLConversionResponse>(
                "/workflow/copilot/convert-yaml-to-blocks",
                {
                  workflow_definition_yaml: workflowDefinitionYaml,
                  workflow_id: saveData.workflow.workflow_id,
                },
              );

            // Construct WorkflowVersion for current state with converted blocks
            const currentVersion: WorkflowVersion = {
              workflow_id: saveData.workflow.workflow_id,
              organization_id: "",
              is_saved_task: saveData.workflow.is_saved_task ?? false,
              is_template: false,
              title: "Current",
              workflow_permanent_id: saveData.workflow.workflow_permanent_id,
              version: saveData.workflow.version ?? 0,
              description: saveData.workflow.description ?? "",
              workflow_definition:
                currentConversionResponse.data.workflow_definition,
              proxy_location: saveData.settings.proxyLocation,
              webhook_callback_url: saveData.settings.webhookCallbackUrl,
              extra_http_headers: saveData.settings.extraHttpHeaders
                ? JSON.parse(saveData.settings.extraHttpHeaders)
                : null,
              persist_browser_session: saveData.settings.persistBrowserSession,
              model: saveData.settings.model,
              totp_verification_url: saveData.workflow.totp_verification_url,
              totp_identifier: null,
              max_screenshot_scrolls: saveData.settings.maxScreenshotScrolls,
              status: saveData.workflow.status,
              created_at: new Date().toISOString(),
              modified_at: new Date().toISOString(),
              deleted_at: null,
              run_with: saveData.settings.runWith,
              cache_key: saveData.settings.scriptCacheKey,
              ai_fallback: saveData.settings.aiFallback,
              adaptive_caching: false,
              code_version:
                saveData.settings.runWith === "code"
                  ? (saveData.settings.codeVersion ?? 2)
                  : null,
              run_sequentially: saveData.settings.runSequentially,
              sequential_key: saveData.settings.sequentialKey,
              folder_id: null,
              import_error: null,
            };

            // Construct fake WorkflowVersion for pending copilot suggestion
            const pendingVersion: WorkflowVersion = {
              ...pendingWorkflow,
              title: "Copilot Suggestion",
            };

            // Handle copilot review close with status
            const handleCopilotReviewClose = (status: CopilotReviewStatus) => {
              if (status === "approve") {
                try {
                  applyWorkflowUpdate(pendingWorkflow);
                } catch (error) {
                  console.error(
                    "Failed to apply copilot workflow",
                    error,
                    pendingWorkflow,
                  );
                  toast({
                    title: "Update failed",
                    description:
                      "Failed to apply workflow update. Please try again.",
                    variant: "destructive",
                  });
                }
              }

              // Close the panel and reopen copilot chat
              setWorkflowPanelState({
                active: false,
                content: "history",
                data: {
                  showComparison: false,
                  version1: undefined,
                  version2: undefined,
                },
              });
              setIsCopilotOpen(true);

              // Clear pending for approve and reject, but not for close
              if (status !== "close") {
                clearPending();
              }
            };

            // Hide chat and show comparison
            setIsCopilotOpen(false);
            setWorkflowPanelState({
              active: false,
              content: "history",
              data: {
                version1: currentVersion,
                version2: pendingVersion,
                showComparison: true,
                mode: "copilot",
                onCopilotReviewClose: handleCopilotReviewClose,
              },
            });
          } catch (error) {
            console.error("Failed to prepare workflow comparison", error);
            toast({
              title: "Comparison failed",
              description:
                "Failed to prepare workflow for comparison. Please try again.",
              variant: "destructive",
            });
          }
        }}
        onWorkflowUpdate={(workflowData, options) => {
          try {
            applyWorkflowUpdate(workflowData, options);
          } catch (error) {
            console.error(
              "Failed to parse and apply workflow",
              error,
              workflowData,
            );
            toast({
              title: "Update failed",
              description: "Failed to apply workflow update. Please try again.",
              variant: "destructive",
            });
          }
        }}
      />
      <DeleteConfirmationDialog
        open={deleteBlockDialogState.open}
        onOpenChange={(open) => {
          if (!open) {
            deleteConfirmCallbackRef.current = null;
            setDeleteBlockDialogState({
              open: false,
              nodeId: null,
              nodeLabel: null,
            });
          }
        }}
        title="Delete Block"
        description={`Are you sure you want to delete "${deleteBlockDialogState.nodeLabel}"?`}
        affectedBlocks={affectedBlocksForDelete}
        onConfirm={() => {
          if (deleteConfirmCallbackRef.current) {
            deleteConfirmCallbackRef.current();
          }
          deleteConfirmCallbackRef.current = null;
          setDeleteBlockDialogState({
            open: false,
            nodeId: null,
            nodeLabel: null,
          });
        }}
      />
    </div>
  );
}

export { CopyText, CopyAndExplainCode, Workspace };
