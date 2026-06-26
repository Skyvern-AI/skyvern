import { AxiosError } from "axios";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
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
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { usePostHog } from "posthog-js/react";

import { getClient } from "@/api/AxiosClient";
import { DebugSessionApiResponse, ProxyLocation } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useMountEffect } from "@/hooks/useMountEffect";
import { useBrowserSessionRateLimit } from "../hooks/useBrowserSessionRateLimit";
import { useDebugSessionQuery } from "../hooks/useDebugSessionQuery";
import { useBlockScriptsQuery } from "@/routes/workflows/hooks/useBlockScriptsQuery";
import { BrowserSessionStream } from "@/routes/browserSessions/BrowserSessionStream";
import { useBrowserStreamingMode } from "@/hooks/useRuntimeConfig";
import {
  StreamModeBadge,
  StreamStatusPanel,
} from "@/routes/streaming/StreamDiagnostics";
import { type BrowserSession as BrowserSessionData } from "@/routes/workflows/types/browserSessionTypes";
import { useCacheKeyValuesQuery } from "../hooks/useCacheKeyValuesQuery";
import {
  DEBUG_SESSION_EXPIRY_STATUS_REFETCH_MS,
  DEBUG_SESSION_EXPIRY_WARNING_THRESHOLD_MS,
  formatBrowserSessionRemainingTime,
  getBrowserSessionRemainingMs,
} from "../hooks/debugSessionLease";
import { useBlockScriptStore } from "@/store/BlockScriptStore";
import { useBlockSidebarWidthStore } from "@/store/BlockSidebarWidthStore";
import { useCacheKeyValueStore } from "@/store/CacheKeyValueStore";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useCopilotActionStore } from "@/store/useCopilotActionStore";
import { useShowAllCodeStore } from "@/store/ShowAllCodeStore";
import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";
import { useWorkflowHistoryAccessStore } from "@/store/WorkflowHistoryAccessStore";
import { useBrowserLoadingFlag } from "../hooks/useBrowserLoadingFlag";

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
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { useWorkflowTitleStore } from "@/store/WorkflowTitleStore";
import { getCode, getOrderedBlockLabels } from "@/routes/workflows/utils";
import { DebuggerBlockRuns } from "@/routes/workflows/debugger/DebuggerBlockRuns";
import { copyText } from "@/util/copyText";
import { isMacPlatform } from "@/util/platform";
import { parseHeaderJson } from "@/util/secretHeaders";
import { cn } from "@/util/utils";

import { FlowRenderer, type FlowRendererProps } from "./FlowRenderer";
import { useCacheKeyValueUrlSync } from "./hooks/useCacheKeyValueUrlSync";
import { useSaveWorkflow } from "./hooks/useSaveWorkflow";
import { useWorkspaceMountInitialization } from "./hooks/useWorkspaceMountInitialization";
import { useWorkflowHistory } from "./hooks/useWorkflowHistory";
import { AppNode, isWorkflowBlockNode, WorkflowBlockNode } from "./nodes";
import { blockTypeFromNode } from "./nodes/blockTypeFromNode";
import { ConditionalNodeData } from "./nodes/ConditionalNode/types";
import { WorkflowParametersPanel } from "./panels/WorkflowParametersPanel";
import { WorkflowCacheKeyValuesPanel } from "./panels/WorkflowCacheKeyValuesPanel";
import {
  WorkflowComparisonPanel,
  type CopilotReviewStatus,
} from "./panels/WorkflowComparisonPanel";
import {
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
import { replayPersistedCollapseVisibility } from "./collapse/applyDescendantCollapseVisibility";
import { useNodeCollapseStore } from "./collapse/useNodeCollapseStore";
import {
  BLOCK_SIDEBAR_WIDTH_VAR,
  HEADER_RIGHT_INSET_CLOSED,
  HEADER_RIGHT_INSET_OPEN,
  isBlockSidebarOpen,
} from "./blockSidebar";
import { useWorkflowEditorMode } from "./hooks/useWorkflowEditorMode";
import { useWorkflowHeaderCollapseStore } from "./useWorkflowHeaderCollapseStore";
import { WorkflowHeader } from "./WorkflowHeader";
import { WorkflowHistoryPanel } from "./panels/WorkflowHistoryPanel";
import { WorkflowSchedulePanel } from "./panels/schedulePanel/WorkflowSchedulePanel";
import { WorkflowVersion } from "../hooks/useWorkflowVersionsQuery";
import { WorkflowSettings } from "../types/workflowTypes";
import { shouldKeepExistingEdgeForInsertion } from "./workflowInsertion";

import { constructCacheKeyValue, getInitialParameters } from "./utils";
import { WorkflowCopilotChat } from "../copilot/WorkflowCopilotChat";
import { useStudioShellContext } from "../studio/StudioShellContext";
import {
  STUDIO_COPILOT_RAIL_WIDTH,
  STUDIO_COPILOT_WIDTH,
} from "../studio/constants";
import { useStudioShellStore } from "@/store/StudioShellStore";
import { WorkflowCopilotButton } from "../copilot/WorkflowCopilotButton";
import { resolveCopilotLiveBrowserReady } from "../copilot/browserReadiness";

import type { WorkflowYAMLConversionResponse } from "../copilot/workflowCopilotTypes";
import "./workspace-styles.css";

function getAxiosErrorDetail(error: unknown): string | undefined {
  if (!(error instanceof AxiosError)) {
    return undefined;
  }

  const data = error.response?.data;
  if (!data || typeof data !== "object" || !("detail" in data)) {
    return undefined;
  }

  const detail = data.detail;
  return typeof detail === "string" ? detail : undefined;
}

const Constants = {
  NewBrowserCooldown: 30000,
} as const;

// How long to poll before recording one rate-limit attempt (60s)
const POLL_ATTEMPT_THRESHOLD_MS = 60_000;

// Marker class for the copilot's gold-ring block-highlight flash. Kept off
// React Flow's `.selected` so a normal editor node click (which sets
// `selected` to open the sidebar) doesn't trigger the flash. Must match the
// selector in reactFlowOverrideStyles.css.
const COPILOT_BLOCK_HIGHLIGHT_CLASS = "sk-copilot-block-highlight";

function setBlockHighlightClass(node: AppNode, on: boolean): AppNode {
  const tokens = (node.className ?? "")
    .split(/\s+/)
    .filter((token) => token && token !== COPILOT_BLOCK_HIGHLIGHT_CLASS);
  if (on) tokens.push(COPILOT_BLOCK_HIGHLIGHT_CLASS);
  const next = tokens.join(" ") || undefined;
  if ((node.className ?? undefined) === next) return node;
  return { ...node, className: next };
}

type Props = Pick<FlowRendererProps, "initialTitle" | "workflow"> & {
  initialNodes: Array<AppNode>;
  initialEdges: Array<Edge>;
  showBrowser?: boolean;
  // When embedded in the Spine+Stage StudioShell, the shell provides the top
  // bar, so Workspace suppresses its own floating WorkflowHeader.
  embedded?: boolean;
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
      <code className="text-xs text-neutral-600 dark:text-neutral-300">
        {text}
      </code>
    </div>
  );
}

function CopyAndExplainCode({
  code,
  showCopy = true,
}: {
  code: string;
  showCopy?: boolean;
}) {
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
      {showCopy ? <CopyText text={code} /> : null}
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
  embedded = false,
  workflow,
}: Props) {
  const { blockLabel, workflowPermanentId } = useParams();
  const { copilotPortalEl: studioCopilotPortalEl } = useStudioShellContext();
  const studioCopilotCollapsed = useStudioShellStore((s) => s.copilotCollapsed);
  const studioSetCopilotCollapsed = useStudioShellStore(
    (s) => s.setCopilotCollapsed,
  );
  const studioSetTab = useStudioShellStore((s) => s.setTab);
  // The studio canvas sits right of the Copilot column; offset the fit by the
  // column width so the chain centers on the whole page, not just the pane.
  const studioCanvasCenterOffset = embedded
    ? studioCopilotCollapsed
      ? STUDIO_COPILOT_RAIL_WIDTH
      : STUDIO_COPILOT_WIDTH
    : 0;
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
  const [searchParams] = useSearchParams();
  const cacheKeyValueParam = searchParams.get("cache-key-value");
  const headlessTurnDrainEnabled = ["1", "true"].includes(
    (searchParams.get("copilotHeadlessTurnDrain") ?? "").toLowerCase(),
  );
  const [timelineMode, setTimelineMode] = useState("wide");
  const [page, setPage] = useState(1);
  const [nudge, setNudge] = useState(false);
  const { workflowPanelState, setWorkflowPanelState, closeWorkflowPanel } =
    useWorkflowPanelStore();
  const showAllCode = useShowAllCodeStore((s) => s.showAllCode);
  const setShowAllCode = useShowAllCodeStore((s) => s.setShowAllCode);
  const cacheKeyValue = useCacheKeyValueStore((s) => s.cacheKeyValue);
  const setExplicitCacheKeyValue = useCacheKeyValueStore((s) => s.setExplicit);
  const cacheKeyValueFilter = useCacheKeyValueStore((s) => s.filter);
  const setCacheKeyValueFilter = useCacheKeyValueStore((s) => s.setFilter);
  const headerCollapsed = useWorkflowHeaderCollapseStore((s) => s.collapsed);
  const editorMode = useWorkflowEditorMode();
  const selectedBlockId = useWorkflowPanelStore((s) => s.selectedBlockId);
  const isNodeLibraryOpen =
    workflowPanelState.active && workflowPanelState.content === "nodeLibrary";
  const blockSidebarOpen = isBlockSidebarOpen(
    editorMode,
    selectedBlockId,
    isNodeLibraryOpen,
  );
  // While collapsed, the pill is offscreen but its WorkflowHeaderCollapseTab
  // (chevron) sits at the bottom edge, centered on the pill. If we let the
  // pill's right inset track blockSidebarOpen while collapsed, the tab snaps
  // sideways every time the user clicks a block. Freeze the inset on the
  // last expanded value so clicks-while-collapsed don't shift the chevron.
  const frozenSidebarOpenRef = useRef(blockSidebarOpen);
  useEffect(() => {
    if (!headerCollapsed) {
      frozenSidebarOpenRef.current = blockSidebarOpen;
    }
  }, [blockSidebarOpen, headerCollapsed]);
  const headerEffectiveSidebarOpen =
    editorMode === "edit"
      ? headerCollapsed
        ? frozenSidebarOpenRef.current
        : blockSidebarOpen
      : false;
  const renderedBlockSidebarWidth = useBlockSidebarWidthStore(
    (s) => s.renderedWidth,
  );
  const handleOnSave = useSaveWorkflow();
  const postHog = usePostHog();
  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);
  const {
    undo: undoWorkflowEdit,
    redo: redoWorkflowEdit,
    captureImmediately: captureWorkflowEditImmediately,
    canUndo: canUndoWorkflowEdit,
    canRedo: canRedoWorkflowEdit,
    historyApplyTrigger,
  } = useWorkflowHistory({ nodes, edges, setNodes, setEdges });

  // Wrappers below invoke the latest closures via this ref so consumers
  // that read between render commit and effect flush see fresh ones.
  // useLayoutEffect (rather than mutating during render) keeps the write
  // out of the purity contract while still landing before paint, so any
  // post-commit reader sees the up-to-date callbacks.
  const historyCallbacksRef = useRef({
    undo: undoWorkflowEdit,
    redo: redoWorkflowEdit,
    captureImmediately: captureWorkflowEditImmediately,
  });
  useLayoutEffect(() => {
    historyCallbacksRef.current = {
      undo: undoWorkflowEdit,
      redo: redoWorkflowEdit,
      captureImmediately: captureWorkflowEditImmediately,
    };
  }, [undoWorkflowEdit, redoWorkflowEdit, captureWorkflowEditImmediately]);

  useEffect(() => {
    useWorkflowHistoryAccessStore.getState().setHistoryAccess({
      canUndo: canUndoWorkflowEdit,
      canRedo: canRedoWorkflowEdit,
      undo: () => historyCallbacksRef.current.undo(),
      redo: () => historyCallbacksRef.current.redo(),
      captureImmediately: () =>
        historyCallbacksRef.current.captureImmediately(),
    });
    // Reset on unmount so the WorkflowHeader (or any other consumer that
    // outlives this Workspace) doesn't fire stale undo/redo callbacks
    // against a workflow we've already navigated away from.
    return () => {
      useWorkflowHistoryAccessStore.getState().reset();
    };
  }, [canUndoWorkflowEdit, canRedoWorkflowEdit]);

  const { getNodes, getEdges } = useReactFlow();
  const { data: workflowRun } = useWorkflowRunQuery();
  const isFinalized = workflowRun ? statusIsFinalized(workflowRun) : false;
  const { browserStreamingMode } = useBrowserStreamingMode();

  const [openCycleBrowserDialogue, setOpenCycleBrowserDialogue] =
    useState(false);
  const [isCopilotOpen, setIsCopilotOpen] = useState(
    () => !!initialCopilotMessage || !initialNodes.some(isWorkflowBlockNode),
  );
  // Open the copilot panel when a code block requests a goal-driven (re)build,
  // so the user can watch the scout and the generated block apply.
  const copilotPendingBuild = useCopilotActionStore(
    (state) => state.pendingBuild,
  );
  useEffect(() => {
    if (copilotPendingBuild) {
      setIsCopilotOpen(true);
    }
  }, [copilotPendingBuild]);
  const [copilotMessageCount, setCopilotMessageCount] = useState(0);
  const copilotButtonRef = useRef<HTMLButtonElement>(null);
  const [readyBrowserSessionId, setReadyBrowserSessionId] = useState<
    string | null
  >(null);
  const [showPowerButton, setShowPowerButton] = useState(true);
  const [reloadKey, setReloadKey] = useState(0);
  const [windowResizeTrigger, setWindowResizeTrigger] = useState(0);
  const [containerResizeTrigger, setContainerResizeTrigger] = useState(0);
  // FlowRenderer reports "pre-layout" → "initial-load" → "ready" as Dagre +
  // the fade-in animation settle. BrowserStream / BrowserSessionStream
  // mount only once we reach "ready" so the VNC websocket handshake +
  // canvas first frame don't compete with the canvas's initial layout
  // pass (heavy on style recalc with many CodeMirror children).
  const [flowLayoutPhase, setFlowLayoutPhase] = useState<
    "pre-layout" | "initial-load" | "ready"
  >("pre-layout");
  const isFlowCanvasReady = flowLayoutPhase === "ready";
  const [isReloading, setIsReloading] = useState(false);
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const [shouldFetchDebugSession, setShouldFetchDebugSession] = useState(false);
  const blockScriptStore = useBlockScriptStore();
  const recordingStore = useRecordingStore();
  const isCdpStreamingMode =
    browserStreamingMode === "cdp" && !recordingStore.isRecording;
  // Record Browser exfiltration requires VNC even when the org default is CDP streaming.
  const preferVncStream =
    browserStreamingMode !== "cdp" || recordingStore.isRecording;
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

  const {
    data: debugSession,
    isError: isDebugSessionError,
    error: debugSessionError,
    refetch: refetchDebugSession,
  } = useDebugSessionQuery({
    workflowPermanentId,
    enabled: shouldFetchDebugSession && !!workflowPermanentId,
    isRateLimited,
    keepAliveBrowserSession: true,
  });

  const activeDebugSession = debugSession ?? null;

  const workflowChangesStore = useWorkflowHasChangesStore();

  const showBreakoutButton =
    activeDebugSession && activeDebugSession.browser_session_id;
  const liveBrowserSessionId = activeDebugSession?.browser_session_id ?? null;
  const showVncBrowserPanel =
    preferVncStream &&
    shouldFetchDebugSession &&
    !isRateLimited &&
    (!activeDebugSession || activeDebugSession.vnc_streaming_supported);
  const showCdpBrowserPanel =
    isCdpStreamingMode && shouldFetchDebugSession && !isRateLimited;
  // Embedded: the shell owns the stream, so bind the copilot once the backend
  // session exists — else it gets a null id and the backend spins a separate browser.
  const copilotRequiresLiveBrowser =
    (showBrowser || embedded) && shouldFetchDebugSession && !isRateLimited;
  // readyBrowserSessionId is keyed to the browser session id rather than a
  // bare boolean: when activeDebugSession's id changes, stale ready state
  // from the previous session cannot leak into the next render.
  const copilotLiveBrowserReady = resolveCopilotLiveBrowserReady({
    displayReady: Boolean(
      readyBrowserSessionId && readyBrowserSessionId === liveBrowserSessionId,
    ),
    hasBackendSession: Boolean(liveBrowserSessionId),
    headlessTurnDrainEnabled: headlessTurnDrainEnabled || embedded,
  });
  const debugSessionExpiryWarningKeyRef = useRef<string | null>(null);

  const { data: liveBrowserSession, dataUpdatedAt: liveBrowserSessionNowMs } =
    useQuery<BrowserSessionData>({
      queryKey: ["browserSession", liveBrowserSessionId],
      queryFn: async () => {
        if (!liveBrowserSessionId) {
          throw new Error("Cannot fetch browser session without an ID");
        }
        const client = await getClient(credentialGetter, "sans-api-v1");
        const response = await client.get<BrowserSessionData>(
          `/browser_sessions/${liveBrowserSessionId}`,
        );
        return response.data;
      },
      enabled:
        Boolean(liveBrowserSessionId) &&
        shouldFetchDebugSession &&
        !isRateLimited,
      refetchInterval: DEBUG_SESSION_EXPIRY_STATUS_REFETCH_MS,
      refetchOnWindowFocus: true,
    });

  const handleLiveBrowserReadyChange = useCallback(
    (ready: boolean, sessionId: string | null) => {
      setReadyBrowserSessionId(ready ? sessionId : null);
    },
    [],
  );

  useBrowserLoadingFlag(shouldFetchDebugSession, readyBrowserSessionId);

  useEffect(() => {
    if (!liveBrowserSession || liveBrowserSession.completed_at) {
      debugSessionExpiryWarningKeyRef.current = null;
      return;
    }

    const remainingMs = getBrowserSessionRemainingMs(
      liveBrowserSession,
      liveBrowserSessionNowMs,
    );
    if (remainingMs !== null && remainingMs <= 0) {
      if (debugSessionExpiryWarningKeyRef.current) {
        toast({
          variant: "destructive",
          title: "Browser session expired",
          description: "Start a new debug browser to continue.",
        });
      }
      debugSessionExpiryWarningKeyRef.current = null;
      return;
    }

    if (
      remainingMs === null ||
      remainingMs > DEBUG_SESSION_EXPIRY_WARNING_THRESHOLD_MS
    ) {
      debugSessionExpiryWarningKeyRef.current = null;
      return;
    }

    const warningKey = `${liveBrowserSession.browser_session_id}:${liveBrowserSession.started_at}:${liveBrowserSession.timeout}`;
    if (debugSessionExpiryWarningKeyRef.current === warningKey) {
      return;
    }

    debugSessionExpiryWarningKeyRef.current = warningKey;
    const remainingTime = formatBrowserSessionRemainingTime(remainingMs);
    toast({
      variant: "warning",
      title: "Browser session expiring soon",
      description: `This debug browser expires in ${remainingTime}. Skyvern renews it automatically while this view is open, but may open a replacement browser if this lease can no longer be renewed.`,
    });
  }, [liveBrowserSession, liveBrowserSessionNowMs]);

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

  // Per-workflow store reset. Earlier revisions did this from
  // `useMountEffect`, but the Workspace instance can be reused across
  // workflows when the parent route doesn't key by workflowPermanentId
  // (e.g. /workflows/A/build → /workflows/B/build); in that case the
  // mount-only initializer would skip and selectedBlockId / showAllCode /
  // sidebar save timestamps would leak from A into B. Keying this on
  // `workflowPermanentId` fires the reset on every workflow change,
  // including a same-instance route swap.
  //
  // Deps are intentionally narrowed to `workflowPermanentId`: same-workflow
  // refetches (e.g. `useWorkflowSave` invalidates `['workflow', id]` after a
  // sidebar save) produce a new `workflow` object reference; if we included
  // `workflow` here the reset would fire mid-session and wipe the user's
  // current block selection, sidebar save state, and cache-key filter.
  // Tracks which wpid the cache-key store was last initialized against, so a
  // same-wpid refetch (object-identity change on `workflow`) doesn't clobber
  // the user's current filter, while an A→B nav still re-initializes once B's
  // payload resolves.
  const cacheKeyInitWpidRef = useRef<string | null>(null);
  useEffect(() => {
    // Studio defaults the selection to the start node on open (legacy keeps it
    // empty); fires only on workflow change, so tab-switch selection persists.
    const startNodeId = embedded
      ? (initialNodes.find((node) => node.type === "start")?.id ?? null)
      : null;
    useWorkflowPanelStore.getState().setSelectedBlockId(startNodeId);
    useShowAllCodeStore.getState().reset();
    useSidebarSaveStateStore.getState().reset();
    cacheKeyInitWpidRef.current = null;
    setReadyBrowserSessionId(null);
    if (workflowPermanentId) {
      queryClient.removeQueries({
        queryKey: ["debugSession", workflowPermanentId],
      });
      setShouldFetchDebugSession(true);
    } else {
      setShouldFetchDebugSession(false);
    }
    // initialNodes/embedded read from the mount closure on purpose; as deps they
    // would re-fire this reset on every workflow refetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflowPermanentId, queryClient]);

  useEffect(() => {
    // Gate on workflow payload matching the route wpid: `useWorkflowQuery`
    // can serve placeholderData from the prior workflow on an A→B nav, and
    // initializing with that stale payload would lock in A's cache-key for
    // B. Wait until B's payload resolves, then init once per wpid.
    if (!workflowPermanentId) return;
    if (workflow.workflow_permanent_id !== workflowPermanentId) return;
    if (cacheKeyInitWpidRef.current === workflowPermanentId) return;
    cacheKeyInitWpidRef.current = workflowPermanentId;
    useCacheKeyValueStore
      .getState()
      .initialize(
        cacheKey === ""
          ? ""
          : cacheKeyValueParam
            ? cacheKeyValueParam
            : constructCacheKeyValue({ codeKey: cacheKey, workflow }),
        !!cacheKeyValueParam,
      );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflowPermanentId, workflow.workflow_permanent_id]);

  // Prune persisted collapse labels every time the workflow id or
  // definition changes. Same Workspace-instance-reuse failure mode as the
  // store-reset effect above: `useMountEffect` skips on A->B nav, leaking
  // orphan labels under B's prefix and risking a future renamed block
  // inheriting a stale collapsed state.
  useEffect(() => {
    if (!workflowPermanentId) return;
    // Walk both loop kinds so collapsed children of for_loop and
    // while_loop both stick around. Conditional branches don't nest in
    // the data structure - their child blocks live in the top-level
    // array referenced by next_block_label.
    const collectAllLabels = (
      blocks: Array<{ label: string; block_type?: string }> | undefined,
    ): Array<string> => {
      if (!blocks) return [];
      const out: Array<string> = [];
      for (const block of blocks) {
        out.push(block.label);
        if (
          block.block_type === "for_loop" ||
          block.block_type === "while_loop"
        ) {
          const loopBlocks = (block as { loop_blocks?: Array<typeof block> })
            .loop_blocks;
          out.push(...collectAllLabels(loopBlocks));
        }
      }
      return out;
    };
    const validLabels = collectAllLabels(workflow.workflow_definition?.blocks);
    useNodeCollapseStore
      .getState()
      .pruneStaleLabels(workflowPermanentId, validLabels);
    // Intentionally exclude `workflow.workflow_definition.blocks` from deps:
    // we only want to prune on workflow-swap (mount or wpid change). Pruning
    // on every block edit drops the collapse entry the instant a user renames
    // a block, before the corresponding write under the new label lands.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflowPermanentId]);

  useWorkspaceMountInitialization({
    cacheKey,
    closeWorkflowPanel,
    queryClient,
    workflowChangesStore,
    workflowPermanentId,
  });

  useCacheKeyValueUrlSync(cacheKeyInitWpidRef.current === workflowPermanentId);

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

  useEffect(() => {
    // Header-anchored panels (cacheKeyValues, parameters, schedules, history)
    // sit at top-[8.5rem] and slide off with the header; nodeLibrary lives in
    // the right sidebar and stays put, so don't auto-dismiss it on collapse.
    if (
      headerCollapsed &&
      workflowPanelState.active &&
      workflowPanelState.content !== "nodeLibrary"
    ) {
      const t = setTimeout(closeWorkflowPanel, 300);
      return () => clearTimeout(t);
    }
  }, [
    headerCollapsed,
    workflowPanelState.active,
    workflowPanelState.content,
    closeWorkflowPanel,
  ]);

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
      resetOnSuccess();

      queryClient.setQueryData(
        ["debugSession", workflowPermanentId],
        newDebugSession,
      );
      void queryClient.invalidateQueries({
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
      ? edges.filter((edge) =>
          shouldKeepExistingEdgeForInsertion(edge, { branch, next, previous }),
        )
      : edges;

    const previousNode = nodes.find((node) => node.id === previous);
    const previousNodeIndex = previousNode
      ? nodes.indexOf(previousNode)
      : nodes.length - 1;

    const newNodesAfter = [
      ...nodes.slice(0, previousNodeIndex + 1),
      ...newNodes,
      ...nodes.slice(previousNodeIndex + 1),
    ];
    workflowChangesStore.setHasChanges(true);
    postHog.capture("builder.block.added", {
      org_id: workflow.organization_id,
      block_type: blockTypeFromNode(node) ?? nodeType,
      position: previousNodeIndex + 1,
    });
    doLayout(newNodesAfter, [...editedEdges, ...newEdges]);
    useWorkflowPanelStore.getState().setSelectedBlockId(id);
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
  ) => {
    setWorkflowPanelState({
      active: true,
      content: "history",
      data: {
        version1: JSON.parse(JSON.stringify(version1)),
        version2: JSON.parse(JSON.stringify(version2)),
        showComparison: true,
      },
    });
  };

  const applyWorkflowUpdate = (
    workflowData: WorkflowVersion,
    options?: { persisted?: boolean },
  ) => {
    const settings: WorkflowSettings = {
      proxyLocation: workflowData.proxy_location ?? ProxyLocation.Residential,
      webhookCallbackUrl: workflowData.webhook_callback_url || "",
      persistBrowserSession: workflowData.persist_browser_session ?? false,
      browserProfileId: workflowData.browser_profile_id ?? null,
      browserProfileKey: workflowData.browser_profile_key ?? null,
      model: workflowData.model ?? null,
      maxScreenshotScrolls: workflowData.max_screenshot_scrolls || 3,
      maxElapsedTimeMinutes: workflowData.max_elapsed_time_minutes ?? null,
      extraHttpHeaders: workflowData.extra_http_headers
        ? JSON.stringify(workflowData.extra_http_headers)
        : null,
      cdpConnectHeaders: workflowData.cdp_connect_headers
        ? JSON.stringify(workflowData.cdp_connect_headers)
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

    const collapsedSet = useNodeCollapseStore.getState().collapsed;
    const wpid = workflowPermanentId ?? "__global__";
    setNodes(
      replayPersistedCollapseVisibility(elements.nodes, wpid, collapsedSet),
    );
    setEdges(elements.edges);

    const initialParameters = getInitialParameters(workflowData);
    useWorkflowParametersStore.getState().setParameters(initialParameters);

    // Sync title so snap-back on Reject reverts the editor's title bar
    // alongside the canvas blocks.
    if (typeof workflowData.title === "string") {
      useWorkflowTitleStore.getState().setTitle(workflowData.title);
    }

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
      browserProfileId: selectedVersion.browser_profile_id ?? null,
      browserProfileKey: selectedVersion.browser_profile_key ?? null,
      model: selectedVersion.model,
      maxScreenshotScrolls: selectedVersion.max_screenshot_scrolls || 3,
      maxElapsedTimeMinutes: selectedVersion.max_elapsed_time_minutes ?? null,
      extraHttpHeaders: selectedVersion.extra_http_headers
        ? JSON.stringify(selectedVersion.extra_http_headers)
        : null,
      cdpConnectHeaders: selectedVersion.cdp_connect_headers
        ? JSON.stringify(selectedVersion.cdp_connect_headers)
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

    const collapsedSet = useNodeCollapseStore.getState().collapsed;
    const wpid = workflowPermanentId ?? "__global__";
    setNodes(
      replayPersistedCollapseVisibility(elements.nodes, wpid, collapsedSet),
    );
    setEdges(elements.edges);
  };

  return (
    <div
      className="relative h-full w-full"
      style={
        {
          [BLOCK_SIDEBAR_WIDTH_VAR]: `${renderedBlockSidebarWidth}px`,
        } as React.CSSProperties
      }
    >
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
      {!embedded && (
        <div
          className={cn(
            "absolute left-6 top-8 z-40 h-20 transition-all duration-300 ease-out",
            headerEffectiveSidebarOpen
              ? HEADER_RIGHT_INSET_OPEN
              : HEADER_RIGHT_INSET_CLOSED,
          )}
          style={{
            transform: headerCollapsed
              ? "translateY(calc(-100% - 2rem))"
              : "translateY(0)",
          }}
        >
          <WorkflowHeader />
        </div>
      )}

      {/* comparison view (takes precedence over both browser and non-browser modes) */}
      {workflowPanelState.data?.showComparison &&
      workflowPanelState.data?.version1 &&
      workflowPanelState.data?.version2 &&
      embedded ? (
        // Studio: a flex row so Agent History docks beside the comparison; the
        // legacy absolute layout below assumes the old full-width editor.
        <div className="flex h-full w-full gap-3 overflow-hidden p-3">
          <div className="min-w-0 flex-1">
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
          {workflowPanelState.active &&
            workflowPanelState.content === "history" && (
              <div className="shrink-0">
                <WorkflowHistoryPanel
                  workflowPermanentId={workflowPermanentId!}
                  onCompare={handleCompareVersions}
                />
              </div>
            )}
        </div>
      ) : workflowPanelState.data?.showComparison &&
        workflowPanelState.data?.version1 &&
        workflowPanelState.data?.version2 ? (
        <div className="relative flex h-full w-full overflow-hidden overflow-x-hidden">
          {/* comparison view */}
          <div
            className="absolute left-6 top-[8.5rem]"
            style={{
              width: workflowPanelState.active
                ? "calc(100% - 32rem)"
                : "calc(100% - 3rem)",
              height: "calc(100vh - 13.5rem)",
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
              className={cn(
                "absolute z-30 transition-all duration-300 ease-out",
                embedded ? "top-3" : "top-[8.5rem]",
                blockSidebarOpen
                  ? HEADER_RIGHT_INSET_OPEN
                  : HEADER_RIGHT_INSET_CLOSED,
              )}
              style={{
                height:
                  workflowPanelState.content === "nodeLibrary"
                    ? "calc(100vh - 14rem)"
                    : "unset",
                transform:
                  !embedded && headerCollapsed
                    ? "translateY(calc(-100% - 8.5rem))"
                    : "translateY(0)",
                opacity: !embedded && headerCollapsed ? 0 : 1,
              }}
            >
              {workflowPanelState.content === "cacheKeyValues" && (
                <WorkflowCacheKeyValuesPanel
                  cacheKeyValues={cacheKeyValues}
                  pending={cacheKeyValuesLoading}
                  scriptKey={workflow.cache_key ?? "default"}
                  filter={cacheKeyValueFilter ?? undefined}
                  onFilterChange={setCacheKeyValueFilter}
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
                  <WorkflowSchedulePanel onClose={closeWorkflowPanel} />
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
                centerOffsetX={studioCanvasCenterOffset}
                embedded={embedded}
                onRequestDeleteNode={handleRequestDeleteNode}
                captureHistoryImmediately={captureWorkflowEditImmediately}
                onAddNode={addNode}
                historyApplyTrigger={historyApplyTrigger}
              />

              {/* sub panels */}
              {workflowPanelState.active && (
                <>
                  {!embedded && workflowPanelState.content === "schedules" && (
                    <div
                      className="absolute inset-0 z-20"
                      onClick={closeWorkflowPanel}
                    />
                  )}
                  <div
                    className={cn(
                      "absolute z-30 transition-all duration-300 ease-out",
                      // Studio's top bar is above the canvas, so the panel drops
                      // from the canvas top; legacy's header is inside it.
                      embedded ? "top-3" : "top-[8.5rem]",
                      blockSidebarOpen
                        ? HEADER_RIGHT_INSET_OPEN
                        : HEADER_RIGHT_INSET_CLOSED,
                    )}
                    style={{
                      height:
                        workflowPanelState.content === "nodeLibrary"
                          ? "calc(100vh - 14rem)"
                          : "unset",
                      transform:
                        !embedded && headerCollapsed
                          ? "translateY(calc(-100% - 8.5rem))"
                          : "translateY(0)",
                      opacity: !embedded && headerCollapsed ? 0 : 1,
                    }}
                  >
                    {workflowPanelState.content === "cacheKeyValues" && (
                      <WorkflowCacheKeyValuesPanel
                        cacheKeyValues={cacheKeyValues}
                        pending={cacheKeyValuesLoading}
                        scriptKey={workflow.cache_key ?? "default"}
                        filter={cacheKeyValueFilter ?? undefined}
                        onFilterChange={setCacheKeyValueFilter}
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
                    {!embedded &&
                      workflowPanelState.content === "parameters" && (
                        <div className="z-30">
                          <WorkflowParametersPanel />
                        </div>
                      )}
                    {!embedded &&
                      workflowPanelState.content === "schedules" && (
                        <div className="z-30">
                          <WorkflowSchedulePanel onClose={closeWorkflowPanel} />
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
                  </div>
                </>
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
          <>
            {workflowPanelState.content === "schedules" && (
              <div
                className="absolute inset-0 z-[15]"
                onClick={closeWorkflowPanel}
              />
            )}
            <div
              className="absolute right-6 top-[8.5rem] z-20 transition-all duration-300 ease-out"
              style={{
                transform: headerCollapsed
                  ? "translateY(calc(-100% - 8.5rem))"
                  : "translateY(0)",
                opacity: headerCollapsed ? 0 : 1,
              }}
            >
              {workflowPanelState.content === "cacheKeyValues" && (
                <WorkflowCacheKeyValuesPanel
                  cacheKeyValues={cacheKeyValues}
                  pending={cacheKeyValuesLoading}
                  scriptKey={workflow.cache_key ?? "default"}
                  filter={cacheKeyValueFilter ?? undefined}
                  onFilterChange={setCacheKeyValueFilter}
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
                <WorkflowSchedulePanel onClose={closeWorkflowPanel} />
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
          </>
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
                    showZoomControls={false}
                    nodes={nodes}
                    edges={edges}
                    setNodes={setNodes}
                    setEdges={setEdges}
                    onNodesChange={onNodesChange}
                    onEdgesChange={onEdgesChange}
                    initialTitle={initialTitle}
                    workflow={workflow}
                    containerResizeTrigger={containerResizeTrigger}
                    onRequestDeleteNode={handleRequestDeleteNode}
                    captureHistoryImmediately={captureWorkflowEditImmediately}
                    onAddNode={addNode}
                    historyApplyTrigger={historyApplyTrigger}
                    onLayoutPhaseChange={setFlowLayoutPhase}
                  />
                </div>
              </div>
              {/* block runs history for current debug session id*/}
              {/*
                pointer-events-none on the wrapper so clicks pass through to
                the FlowRenderer's bottom-left Controls (FitView, Lock,
                GlobalCollapse) that sit in the same corner; the actual
                debugger chip re-enables pointer events on itself.
              */}
              <div className="pointer-events-none absolute bottom-[0.5rem] left-[0.75rem] flex w-full items-start justify-center [&>*]:pointer-events-auto">
                <DebuggerBlockRuns />
              </div>
            </div>

            <div className="skyvern-split-right relative flex h-full items-end justify-center bg-neutral-50 p-4 pl-6 dark:bg-background">
              {/* node library sub panel */}
              {/* browser & timeline */}
              <div className="flex h-[calc(100%_-_8rem)] w-full gap-6">
                {isRateLimited && shouldFetchDebugSession && (
                  <div
                    data-testid="browser-rate-limit-message"
                    className="flex h-full w-[calc(100%_-_6rem)] flex-1 items-center justify-center"
                  >
                    <div className="flex max-w-md flex-col items-center justify-center gap-4 rounded-md border border-neutral-200 bg-white p-8 text-center dark:border-neutral-800 dark:bg-neutral-950">
                      <p className="text-sm text-neutral-600 dark:text-neutral-300">
                        Failed to load a browser. We have a high demand for
                        browsers right now. The browser will become available
                        again automatically in ~30 minutes. If the issue
                        persists, please contact support.
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
                )}

                {/* Live browser: mode comes from BROWSER_STREAMING_MODE / runtime config */}
                {showVncBrowserPanel && (
                  <div className="skyvern-vnc-browser flex h-full w-[calc(100%_-_6rem)] flex-1 flex-col items-center justify-center">
                    <div key={reloadKey} className="w-full flex-1">
                      {!liveBrowserSessionId ? (
                        isDebugSessionError ? (
                          <StreamStatusPanel
                            diagnostic={{
                              title: "Could not start browser session",
                              detail:
                                getAxiosErrorDetail(debugSessionError) ??
                                "The backend rejected the browser session request.",
                              hint: "Local dev only supports one browser at a time. Retry after closing other agents.",
                            }}
                          >
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() => void refetchDebugSession()}
                            >
                              Retry
                            </Button>
                          </StreamStatusPanel>
                        ) : (
                          <StreamStatusPanel
                            diagnostic={{
                              title: "Starting browser session",
                              detail:
                                "Creating a debug browser session for this agent.",
                            }}
                          />
                        )
                      ) : isFlowCanvasReady ? (
                        <BrowserStream
                          key={liveBrowserSessionId}
                          exfiltrate={recordingStore.isRecording}
                          interactive={true}
                          browserSessionId={liveBrowserSessionId}
                          showControlButtons={true}
                          resizeTrigger={windowResizeTrigger}
                          isExecuting={!!workflowRun && !isFinalized}
                          onReadyChange={handleLiveBrowserReadyChange}
                        />
                      ) : (
                        <StreamStatusPanel
                          diagnostic={{
                            title: "Preparing live browser",
                            detail:
                              "Waiting for the workflow canvas to finish loading.",
                          }}
                        />
                      )}
                    </div>
                    <footer className="flex h-[2rem] w-full items-center justify-start gap-4 text-muted-foreground">
                      <WorkflowCopilotButton
                        ref={copilotButtonRef}
                        messageCount={copilotMessageCount}
                        onClick={() => setIsCopilotOpen((prev) => !prev)}
                      />
                      <div className="flex items-center gap-2 text-muted-foreground">
                        <GlobeIcon /> Live Browser
                        <StreamModeBadge mode="vnc" />
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

                {showCdpBrowserPanel && (
                  <div className="skyvern-screenshot-browser flex h-full w-[calc(100%_-_6rem)] flex-1 flex-col items-center justify-center">
                    <div
                      key={reloadKey}
                      className="flex w-full flex-1 items-center justify-center"
                    >
                      {!liveBrowserSessionId ? (
                        isDebugSessionError ? (
                          <StreamStatusPanel
                            diagnostic={{
                              title: "Could not start browser session",
                              detail:
                                getAxiosErrorDetail(debugSessionError) ??
                                "The backend rejected the browser session request.",
                              hint: "Local dev only supports one browser at a time. Retry after closing other agents.",
                            }}
                          >
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() => void refetchDebugSession()}
                            >
                              Retry
                            </Button>
                          </StreamStatusPanel>
                        ) : (
                          <StreamStatusPanel
                            diagnostic={{
                              title: "Starting browser session",
                              detail:
                                "Creating a debug browser session for this agent.",
                            }}
                          />
                        )
                      ) : isFlowCanvasReady ? (
                        <BrowserSessionStream
                          browserSessionId={liveBrowserSessionId}
                          interactive={true}
                          showControlButtons={true}
                          onReadyChange={handleLiveBrowserReadyChange}
                        />
                      ) : (
                        <StreamStatusPanel
                          diagnostic={{
                            title: "Preparing live browser",
                            detail:
                              "Waiting for the workflow canvas to finish loading.",
                          }}
                        />
                      )}
                    </div>
                    <footer className="flex h-[2rem] w-full items-center justify-start gap-4 text-muted-foreground">
                      <WorkflowCopilotButton
                        ref={copilotButtonRef}
                        messageCount={copilotMessageCount}
                        onClick={() => setIsCopilotOpen((prev) => !prev)}
                      />
                      <div className="flex items-center gap-2 text-muted-foreground">
                        <GlobeIcon /> Live Browser
                        <StreamModeBadge mode="cdp" />
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

                {activeDebugSession &&
                  preferVncStream &&
                  !activeDebugSession.vnc_streaming_supported &&
                  !showCdpBrowserPanel && (
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
                      "group relative h-full w-[25rem] translate-x-[-20.5rem] bg-neutral-50 transition-all dark:bg-background",
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
                          "pointer-events-none relative flex h-full w-full flex-col items-start overflow-hidden bg-neutral-50 dark:bg-background",
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
        isOpen={
          embedded ? !studioCopilotCollapsed : showBrowser && isCopilotOpen
        }
        docked={embedded}
        portalTarget={embedded ? studioCopilotPortalEl : undefined}
        onCollapse={
          embedded ? () => studioSetCopilotCollapsed(true) : undefined
        }
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
        onBlockSelect={(blockLabel) => {
          const matches = (node: AppNode) =>
            (node.data as { label?: string } | undefined)?.label === blockLabel;
          setNodes((prev) =>
            prev.map((node) => setBlockHighlightClass(node, matches(node))),
          );
          // Auto-clear so the gold-ring flash animation re-triggers on the
          // next select instead of the highlight sticking.
          setTimeout(() => {
            setNodes((prev) =>
              prev.map((node) =>
                matches(node) ? setBlockHighlightClass(node, false) : node,
              ),
            );
          }, 1500);
        }}
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

            let extraHttpHeaders: Record<string, string> | null = null;
            if (saveData.settings.extraHttpHeaders) {
              try {
                extraHttpHeaders = parseHeaderJson(
                  saveData.settings.extraHttpHeaders,
                );
              } catch (error) {
                toast({
                  title: "Error",
                  description: "Invalid JSON format in extra http headers",
                  variant: "destructive",
                });
                return;
              }
            }

            let cdpConnectHeaders: Record<string, string> | null = null;
            if (saveData.settings.cdpConnectHeaders) {
              try {
                cdpConnectHeaders = parseHeaderJson(
                  saveData.settings.cdpConnectHeaders,
                );
              } catch (error) {
                toast({
                  title: "Error",
                  description: "Invalid JSON format in cdp connect headers",
                  variant: "destructive",
                });
                return;
              }
            }

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
              extra_http_headers: extraHttpHeaders,
              cdp_connect_headers: cdpConnectHeaders,
              persist_browser_session: saveData.settings.persistBrowserSession,
              browser_profile_id: saveData.settings.browserProfileId,
              browser_profile_key: saveData.settings.browserProfileKey,
              model: saveData.settings.model,
              totp_verification_url: saveData.workflow.totp_verification_url,
              totp_identifier: null,
              max_screenshot_scrolls: saveData.settings.maxScreenshotScrolls,
              max_elapsed_time_minutes:
                saveData.settings.maxElapsedTimeMinutes ?? null,
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
                    "Failed to apply copilot agent",
                    error,
                    pendingWorkflow,
                  );
                  toast({
                    title: "Update failed",
                    description:
                      "Failed to apply agent update. Please try again.",
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

            // Hide chat and show comparison. The comparison renders on the
            // editor canvas, so surface the editor tab when docked in the studio.
            setIsCopilotOpen(false);
            if (embedded) {
              studioSetTab("editor");
            }
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
            console.error("Failed to prepare agent comparison", error);
            toast({
              title: "Comparison failed",
              description:
                "Failed to prepare agent for comparison. Please try again.",
              variant: "destructive",
            });
          }
        }}
        onWorkflowUpdate={(workflowData, options) => {
          try {
            applyWorkflowUpdate(workflowData, options);
          } catch (error) {
            console.error(
              "Failed to parse and apply agent",
              error,
              workflowData,
            );
            toast({
              title: "Update failed",
              description: "Failed to apply agent update. Please try again.",
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
