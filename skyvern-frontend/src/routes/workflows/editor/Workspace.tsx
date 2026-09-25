import {
  bindCopilotReviewClose,
  captureEditorState,
  restoreEditorState,
  type EditorStateSnapshot,
} from "./editorStateSnapshot";
import {
  isLockedByOther,
  registerEditorOwner,
  unregisterEditorOwner,
  reconcileYamlDraftAfterGraphChange,
  useWorkflowYamlEditorStore,
  isYamlCommitRevisionCurrent,
  persistYamlCommitIfCurrent,
  isWorkflowYamlDirty,
  beginYamlCommit,
  createYamlCommitOwner,
  finishYamlCommit,
  isYamlCommitOwnerCurrent,
  runWorkflowAuthoringAction,
  refuseMutationDuringYamlCommit,
  type YamlCommitOwner,
} from "@/store/WorkflowYamlEditorStore";
import { apiWorkflowToSettings } from "@/routes/workflows/editor/apiWorkflowToSettings";
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
import { parse as parseYAML, stringify as convertToYAML } from "yaml";
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
import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";
import { useReactFlow, Edge } from "@xyflow/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { usePostHog } from "posthog-js/react";

import { getClient } from "@/api/AxiosClient";
import { isPaymentRequiredError } from "@/api/paymentRequired";
import { DebugSessionApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useMountEffect } from "@/hooks/useMountEffect";
import { useBrowserSessionRateLimit } from "../hooks/useBrowserSessionRateLimit";
import { useActiveRunSessionQuery } from "../hooks/useActiveRunSessionQuery";
import {
  shouldPollDebugSessionInvalidation,
  useDebugSessionQuery,
} from "../hooks/useDebugSessionQuery";
import { useIsGlobalWorkflow } from "../hooks/useIsGlobalWorkflow";
import { resolveWorkspaceBrowserSessionBindings } from "./browserSessionBindings";
import { useBlockScriptsQuery } from "@/routes/workflows/hooks/useBlockScriptsQuery";
import { BrowserSessionStream } from "@/routes/browserSessions/BrowserSessionStream";
import { useStreamTransport } from "@/hooks/useRuntimeConfig";
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
import { useRecordedBlocksStore } from "@/store/RecordedBlocksStore";
import { useWorkflowSettingsStore } from "@/store/WorkflowSettingsStore";
import { useStudioPaneDefaults } from "../studio/StudioPaneDefaultsContext";
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
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { AffectedBlocksNotice } from "./AffectedBlocksNotice";
import { BrowserStream } from "@/components/BrowserStream";
import { useApplyRecordedBlocks } from "@/routes/workflows/editor/recording/useApplyRecordedBlocks";
import {
  runIsLogicallyFinal,
  runIsExecuting,
} from "@/routes/workflows/workflowRun/runRetryState";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { DebuggerRun } from "@/routes/workflows/debugger/DebuggerRun";
import { DebuggerRunMinimal } from "@/routes/workflows/debugger/DebuggerRunMinimal";
import { RecentActivityRunSelector } from "@/routes/workflows/debugger/recentActivity/RecentActivityRunSelector";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import {
  BranchContext,
  useWorkflowPanelStore,
} from "@/store/WorkflowPanelStore";
import {
  useWorkflowHasChangesStore,
  usePendingWorkflowSaveRecovery,
  useWorkflowSave,
  type WorkflowSaveData,
} from "@/store/WorkflowHasChangesStore";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { useWorkflowSnapshotStore } from "@/store/WorkflowSnapshotStore";
import {
  applySettingsPatch,
  resolveFinallyBlockLabel,
  buildWorkflowYamlDocument,
  restoreWorkflowCopilotSettings,
} from "./workflowYamlDocument";
import {
  applyYamlCommitMetadata,
  useWorkflowTitleStore,
} from "@/store/WorkflowTitleStore";
import {
  getCode,
  getOrderedBlockLabels,
  shouldPollForGeneratedCode,
} from "@/routes/workflows/utils";
import { copyText } from "@/util/copyText";
import { isMacPlatform } from "@/util/platform";
import { parseHeaderJson } from "@/util/secretHeaders";
import { getJsonParseErrorDetail } from "@/util/jsonParseError";
import { cn } from "@/util/utils";
import { FlowRenderer, type FlowRendererProps } from "./FlowRenderer";
import { useCacheKeyValueUrlSync } from "./hooks/useCacheKeyValueUrlSync";
import {
  getInitialSelectedBlockId,
  useSelectedBlockUrlSync,
} from "./hooks/useSelectedBlockUrlSync";
import {
  confirmCodeCacheDeletion,
  useSaveWorkflow,
} from "./hooks/useSaveWorkflow";
import {
  useWorkspaceDeferredEditCleanup,
  useWorkspaceMountInitialization,
} from "./hooks/useWorkspaceMountInitialization";
import { useWorkflowHistory } from "./hooks/useWorkflowHistory";
import { AppNode, isWorkflowBlockNode, WorkflowBlockNode } from "./nodes";
import { blockTypeFromNode } from "./nodes/blockTypeFromNode";
import { ConditionalNodeData } from "./nodes/ConditionalNode/types";
import { WorkflowParametersPanel } from "./panels/WorkflowParametersPanel";
import { WorkflowCacheKeyValuesPanel } from "./panels/WorkflowCacheKeyValuesPanel";
import { WorkflowComparisonPanel } from "./panels/WorkflowComparisonPanel";
import {
  getElements,
  convert,
  useWorkflowGraphState,
  getAffectedBlocks,
  getOutputParameterKey,
  nodeAdderNode,
  createNode,
  defaultEdge,
  generateNodeLabel,
  layout,
  startNode,
  getWorkflowBlocks,
  getWorkflowErrors,
  upgradeWorkflowDefinitionToVersionTwo,
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
import { WorkflowDefinition, WorkflowSettings } from "../types/workflowTypes";
import { useAgentsPathMatch } from "../useAgentsPathMatch";
import { shouldKeepExistingEdgeForInsertion } from "./workflowInsertion";

import { constructCacheKeyValue, getInitialParameters } from "./utils";
import {
  WorkflowCopilotChat,
  type WorkflowUpdateOptions,
} from "../copilot/WorkflowCopilotChat";
import { useStudioRunId } from "../studio/useStudioRunId";
import { copilotRunId } from "./copilotRunId";
import {
  shouldOpenCopilotPaneForHandoff,
  useDiscoverCopilotPromptRecovery,
  withoutDiscoverViaParam,
} from "../discoverCopilotHandoff";
import { useStudioShellContext } from "../studio/StudioShellContext";
import { StudioShellPanelPortal } from "../studio/StudioShellPanelPortal";
import { useRecordingLauncherStore } from "@/store/useRecordingLauncherStore";
import type { CopilotAttachedFile } from "@/routes/workflows/copilot/workflowCopilotTypes";
import { useSopToBlocksMutation } from "../hooks/useSopToBlocksMutation";
import {
  applySopResultAtCurrentAppend,
  resolveAppendInsertionPoint,
  resolveWorkspaceAuthoringActionAvailability,
} from "./workspaceAuthoringActions";
import { paneWidthsKey } from "../studio/paneLayout";
import { useStudioPanes } from "../studio/useStudioPanes";
import { WorkflowCopilotButton } from "../copilot/WorkflowCopilotButton";
import { resolveCopilotLiveBrowserReady } from "../copilot/browserReadiness";

import type {
  CopilotProductAction,
  WorkflowYAMLConversionResponse,
} from "../copilot/workflowCopilotTypes";
import {
  WorkflowYamlEditor,
  WorkflowSavePendingNotice,
} from "./WorkflowYamlEditor";
import { YamlModeToggle } from "./YamlModeToggle";
import { useWorkflowYamlEditorLifecycle } from "./hooks/useWorkflowYamlEditorLifecycle";
import {
  type MetadataPatch,
  workflowVersionFromSaveData,
  yamlCommitInputs,
} from "./workflowVersionFromSaveData";
import "./workspace-styles.css";

function readCopilotAttachedFiles(
  value: unknown,
): Array<CopilotAttachedFile> | undefined {
  if (!Array.isArray(value)) return undefined;
  const files = value.filter(
    (item): item is CopilotAttachedFile =>
      typeof item === "object" &&
      item !== null &&
      typeof (item as CopilotAttachedFile).file_id === "string" &&
      typeof (item as CopilotAttachedFile).filename === "string",
  );
  return files.length > 0 ? files : undefined;
}

function readCopilotProductAction(value: unknown): CopilotProductAction | null {
  if (!value || typeof value !== "object") {
    return null;
  }
  const candidate = value as {
    kind?: unknown;
    workflowRunId?: unknown;
    nonce?: unknown;
  };
  if (typeof candidate.nonce !== "string") {
    return null;
  }
  if (
    candidate.kind === "diagnose_run" &&
    typeof candidate.workflowRunId === "string"
  ) {
    return {
      kind: "diagnose_run",
      workflowRunId: candidate.workflowRunId,
      nonce: candidate.nonce,
    };
  }
  return candidate.kind === "refine_recording"
    ? { kind: "refine_recording", nonce: candidate.nonce }
    : null;
}

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

// eslint-disable-next-line react-refresh/only-export-components -- Exercise the production apply callback without mounting the entire workspace.
export function useWorkspaceCopilotUpdate({
  applyWorkflowUpdate,
}: {
  applyWorkflowUpdate: (
    workflow: WorkflowVersion,
    options: WorkflowUpdateOptions & { userDriven: boolean },
  ) => boolean | void;
}) {
  return (workflowData: WorkflowVersion, options?: WorkflowUpdateOptions) => {
    try {
      // All Copilot-driven applies are user edits (mid-turn draft, accept,
      // snap-back); only version-restore/load call applyWorkflowUpdate
      // without this and stay a clean baseline.
      if (
        applyWorkflowUpdate(workflowData, { ...options, userDriven: true }) ===
        false
      )
        throw new Error("The editor refused the Copilot update");
    } catch (error) {
      console.error("Failed to parse and apply agent", error, workflowData);
      toast({
        title: "Update failed",
        description: "Failed to apply agent update. Please try again.",
        variant: "destructive",
      });
      throw error;
    }
  };
}

function Workspace({
  initialNodes,
  initialEdges,
  initialTitle,
  showBrowser = false,
  embedded = false,
  workflow: loadedWorkflow,
}: Props) {
  const [acceptedWorkflow, setAcceptedWorkflow] =
    useState<WorkflowVersion | null>(null);
  const [parameterBaselines, setParameterBaselines] = useState<
    Record<string, WorkflowVersion["workflow_definition"]["parameters"]>
  >({});
  const workflow =
    acceptedWorkflow?.workflow_permanent_id ===
      loadedWorkflow.workflow_permanent_id &&
    acceptedWorkflow.version > loadedWorkflow.version
      ? acceptedWorkflow
      : loadedWorkflow;
  const { blockLabel } = useParams();
  const workflowPermanentId = useWorkflowPermanentId();
  const { copilotPortalEl: studioCopilotPortalEl } = useStudioShellContext();
  const { panes: studioPanes, openPane: openStudioPane } = useStudioPanes();
  const { paneWidths: studioPaneWidths, entryId: studioEntryId } =
    useStudioPaneDefaults();
  const studioCopilotOpen = studioPanes.includes("copilot");
  const location = useLocation();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const locationState = location.state as {
    copilotMessage?: unknown;
    copilotAttachedFiles?: unknown;
    copilotAction?: unknown;
  } | null;
  const routeInitialCopilotAttachments = useMemo(
    () => readCopilotAttachedFiles(locationState?.copilotAttachedFiles),
    [locationState?.copilotAttachedFiles],
  );
  const routeInitialCopilotMessage =
    typeof locationState?.copilotMessage === "string"
      ? locationState.copilotMessage
      : null;
  // Identity has to survive re-renders: this gates a timer that re-arms whenever it changes.
  const copilotActionState = locationState?.copilotAction;
  const initialCopilotAction = useMemo(
    () => readCopilotProductAction(copilotActionState),
    [copilotActionState],
  );
  const { storedInitialCopilotMessage, clearStoredInitialCopilotMessage } =
    useDiscoverCopilotPromptRecovery({
      shouldRead: searchParams.get("via") === "discover",
      workflowPermanentId,
    });
  const initialCopilotMessage = useMemo(
    () => routeInitialCopilotMessage ?? storedInitialCopilotMessage,
    [routeInitialCopilotMessage, storedInitialCopilotMessage],
  );
  const handleInitialCopilotMessageConsumed = useCallback(() => {
    if (!initialCopilotMessage && !initialCopilotAction) return;
    clearStoredInitialCopilotMessage();
    navigate(location.pathname + withoutDiscoverViaParam(location.search), {
      replace: true,
      state: null,
    });
  }, [
    initialCopilotMessage,
    initialCopilotAction,
    clearStoredInitialCopilotMessage,
    location.pathname,
    location.search,
    navigate,
  ]);
  // A handoff (Discover or the onboarding CTA) lands with the prompt seeded but
  // only the default editor+browser panes open; open the Copilot pane once on
  // mount so the handed-off prompt is visible (the non-embedded editor opens
  // Copilot via isCopilotOpen's initializer below instead). Thread the handoff
  // route state through the pane-open navigation: the CTA seeds the prompt via
  // location.state alone (Discover also has a sessionStorage fallback), so a
  // state-wiping open would drop it before the Copilot consumes it.
  useMountEffect(() => {
    if (
      shouldOpenCopilotPaneForHandoff({
        embedded,
        hasInitialCopilotMessage: Boolean(initialCopilotMessage),
        copilotPaneOpen: studioCopilotOpen,
      })
    ) {
      openStudioPane("copilot", { state: location.state });
    }
  });
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
  const isEditRoute = useAgentsPathMatch("/:workflowPermanentId/edit") !== null;
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
  const saveWorkflow = useWorkflowSave({ status: "published" });
  const yamlCommitOwnerRef = useRef<YamlCommitOwner | null>(null);
  useWorkspaceDeferredEditCleanup(workflow.workflow_permanent_id);
  useLayoutEffect(() => {
    const owner = createYamlCommitOwner(workflow.workflow_permanent_id);
    yamlCommitOwnerRef.current = owner;
    registerEditorOwner(owner);
    useWorkflowTitleStore
      .getState()
      .startCopilotMetadata(owner.workflowPermanentId);
    return () => {
      unregisterEditorOwner(owner);
      const titleStore = useWorkflowTitleStore.getState();
      titleStore.clearCopilotMetadata(owner.workflowPermanentId);
      titleStore.resetTitleSession(owner.workflowPermanentId);
      titleStore.resetDescriptionSession(owner.workflowPermanentId);
      useWorkflowParametersStore
        .getState()
        .resetParametersSession(owner.workflowPermanentId);
      if (yamlCommitOwnerRef.current === owner)
        yamlCommitOwnerRef.current = null;
    };
  }, [workflowPermanentId, workflow.workflow_permanent_id]);
  // Global/read-only workflows can't be edited in place (the header offers
  // "Make a Copy"), so the YAML editor must not open or commit for them.
  const isGlobalWorkflow = useIsGlobalWorkflow();
  const postHog = usePostHog();
  const { getNodes, getEdges } = useReactFlow();
  const {
    nodes,
    edges,
    setNodes,
    setEdges,
    onNodesChange,
    onEdgesChange,
    updateNodes,
    updateEdges,
  } = useWorkflowGraphState(initialNodes, initialEdges);
  const {
    undo: applyUndo,
    redo: applyRedo,
    captureImmediately: captureWorkflowEditImmediately,
    canUndo: canUndoWorkflowEdit,
    canRedo: canRedoWorkflowEdit,
    historyApplyTrigger,
  } = useWorkflowHistory({ nodes, edges, setNodes, setEdges });
  const [restoreApplyTrigger, setRestoreApplyTrigger] = useState(0);
  const captureLiveEditorState = (): EditorStateSnapshot => {
    const titles = useWorkflowTitleStore.getState();
    const changes = useWorkflowHasChangesStore.getState();
    return captureEditorState({
      workflowPermanentId: workflow.workflow_permanent_id,
      nodes,
      edges,
      parameters: useWorkflowParametersStore.getState().parameters,
      parameterBaseline:
        parameterBaselines[workflow.workflow_permanent_id] ??
        workflow.workflow_definition.parameters,
      title: titles.title,
      titleHasBeenGenerated: titles.titleHasBeenGenerated,
      description: titles.description,
      hasChanges: changes.hasChanges,
      saveGeneration: changes.saveGeneration,
    });
  };
  const restoreLiveEditorState = (snapshot: EditorStateSnapshot) => {
    const result = restoreEditorState(snapshot, {
      workflowPermanentId: workflow.workflow_permanent_id,
      setNodes: (nodes) => setNodes(nodes, false),
      setEdges: (edges) => setEdges(edges, false),
      parametersStore: useWorkflowParametersStore.getState(),
      titleStore: useWorkflowTitleStore.getState(),
      changesStore: useWorkflowHasChangesStore.getState(),
      collapseStore: useNodeCollapseStore.getState(),
      restoreOwnership: (workflowPermanentId) => {
        useWorkflowParametersStore.setState({
          parametersWorkflowPermanentId: workflowPermanentId,
        });
        useWorkflowTitleStore.setState({
          titleWorkflowPermanentId: workflowPermanentId,
          descriptionWorkflowPermanentId: workflowPermanentId,
        });
      },
      scheduleLayout: () => setRestoreApplyTrigger((value) => value + 1),
      isLockedByOther,
    });
    if (result === "restored" && snapshot.parameterBaseline)
      setParameterBaselines((current) => ({
        ...current,
        [snapshot.workflowPermanentId]: structuredClone(
          snapshot.parameterBaseline!,
        ),
      }));
    return result;
  };
  const undoWorkflowEdit = useCallback(() => {
    if (refuseMutationDuringYamlCommit()) return;
    applyUndo();
  }, [applyUndo]);
  const redoWorkflowEdit = useCallback(() => {
    if (refuseMutationDuringYamlCommit()) return;
    applyRedo();
  }, [applyRedo]);

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

  const { data: workflowRun } = useWorkflowRunQuery();
  const studioRunId = useStudioRunId();
  const isFinalized = workflowRun ? runIsLogicallyFinal(workflowRun) : false;

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
  useEffect(() => {
    if (initialCopilotAction) {
      setIsCopilotOpen(true);
    }
  }, [initialCopilotAction]);
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
  const yamlEditorActive = useWorkflowYamlEditorStore((s) => s.active);
  const yamlEditorDirty = useWorkflowYamlEditorStore(
    (s) => s.active && isWorkflowYamlDirty(s),
  );
  // hasChanges at the moment YAML mode opened, so reverting a YAML edit
  // restores the pre-YAML dirty state instead of leaving it stuck true.
  const yamlEntryHadChangesRef = useRef(false);
  const [shouldFetchDebugSession, setShouldFetchDebugSession] = useState(false);
  const [isCopilotTurnActive, setIsCopilotTurnActive] = useState(false);
  const blockScriptStore = useBlockScriptStore();
  const recordingStore = useRecordingStore();
  const finallyBlockLabel = useWorkflowSettingsStore(
    (state) => state.finallyBlockLabel,
  );
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
      if (refuseMutationDuringYamlCommit()) return;
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
    Boolean(workflowRun) &&
    shouldPollForGeneratedCode(workflow, isFinalized, hasPublishedScript);

  const { data: blockScriptsPending } = useBlockScriptsQuery({
    cacheKey,
    cacheKeyValue,
    enabled: isGeneratingCode,
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
  const debugSessionPaymentRequired = isPaymentRequiredError(debugSessionError);
  const debugSessionErrorPanel = (
    <StreamStatusPanel
      diagnostic={
        debugSessionPaymentRequired
          ? {
              title: "Out of credits",
              detail:
                getAxiosErrorDetail(debugSessionError) ??
                "More credits are required to start a browser session.",
              hint: "Upgrade your plan in Billing, then return here to start the browser session.",
            }
          : {
              title: "Could not start browser session",
              detail:
                getAxiosErrorDetail(debugSessionError) ??
                "The backend rejected the browser session request.",
              hint: "Local dev only supports one browser at a time. Retry after closing other agents.",
            }
      }
    >
      <Button
        variant="outline"
        size="sm"
        onClick={() => {
          if (debugSessionPaymentRequired) {
            navigate("/billing");
          } else {
            void refetchDebugSession();
          }
        }}
      >
        {debugSessionPaymentRequired ? "Go to Billing" : "Retry"}
      </Button>
    </StreamStatusPanel>
  );
  const { data: viewerState } = useActiveRunSessionQuery({
    workflowPermanentId,
    enabled:
      shouldFetchDebugSession && Boolean(workflowPermanentId) && !isRateLimited,
    isTurnActive: isCopilotTurnActive,
  });

  const activeDebugSession = debugSession ?? null;

  const { streamTransport } = useStreamTransport(
    activeDebugSession?.browser_session_id,
  );
  // Recording stays on the session's own transport: a cdp-transport session is one
  // with no relayable RFB endpoint, so swapping to VNC to record kills the view.
  const isCdpStreamingMode = streamTransport === "cdp";
  const preferVncStream = streamTransport !== "cdp";

  const workflowChangesStore = useWorkflowHasChangesStore();

  useLayoutEffect(() => {
    const hydrateSavedSettings = (
      savedWorkflow: WorkflowVersion,
      options?: { hydrateGraph?: boolean },
    ) => {
      const settings = apiWorkflowToSettings(savedWorkflow);
      setParameterBaselines((current) => ({
        ...current,
        [savedWorkflow.workflow_permanent_id]:
          savedWorkflow.workflow_definition.parameters,
      }));
      if (options?.hydrateGraph) {
        const elements = getElements(
          savedWorkflow.workflow_definition.blocks,
          settings,
          true,
        );
        updateNodes(
          replayPersistedCollapseVisibility(
            elements.nodes,
            savedWorkflow.workflow_permanent_id,
            useNodeCollapseStore.getState().collapsed,
          ),
        );
        updateEdges(elements.edges);
        useWorkflowParametersStore
          .getState()
          .setParameters(getInitialParameters(savedWorkflow), {
            fromYamlCommit: true,
            workflowPermanentId: savedWorkflow.workflow_permanent_id,
          });
        useWorkflowSnapshotStore.getState().clearSnapshot();
        return;
      }
      // Saved settings are the clean baseline; skip user-edit tracking.
      updateNodes((current) =>
        current.map((node) => {
          if (node.type !== "start" || !node.data.withWorkflowSettings)
            return node;
          const hydrated = getElements([], settings, node.data.editable)
            .nodes[0];
          if (!hydrated || hydrated.type !== "start") return node;
          return {
            ...node,
            data: {
              ...node.data,
              ...hydrated.data,
              showCode: node.data.showCode,
            },
          };
        }),
      );
    };
    useWorkflowHasChangesStore.setState({ hydrateSavedSettings });
    return () => {
      if (
        useWorkflowHasChangesStore.getState().hydrateSavedSettings ===
        hydrateSavedSettings
      )
        useWorkflowHasChangesStore.setState({ hydrateSavedSettings: null });
    };
  }, [updateNodes, updateEdges]);

  usePendingWorkflowSaveRecovery(workflow);

  const showBreakoutButton =
    activeDebugSession && activeDebugSession.browser_session_id;
  const { debugBrowserSessionId, displayBrowserSessionId } =
    resolveWorkspaceBrowserSessionBindings(
      activeDebugSession?.browser_session_id ?? null,
      viewerState?.active_run_session_id ?? null,
    );
  const activeRunSessionIdRef = useRef<string | null>(null);
  activeRunSessionIdRef.current = viewerState?.active_run_session_id ?? null;
  const showVncBrowserPanel =
    preferVncStream &&
    shouldFetchDebugSession &&
    !isRateLimited &&
    (!activeDebugSession || activeDebugSession.vnc_streaming_supported);
  const showCdpBrowserPanel =
    isCdpStreamingMode && shouldFetchDebugSession && !isRateLimited;
  // Recording is session-scoped: the stream opts out of reset-on-unmount (it can
  // remount while the session persists), so the debug session owns the reset
  // here — embedded, StudioBrowserStream owns it. The id guard keeps the
  // null -> first-id transition from clearing a recording that started before
  // the session resolved.
  useEffect(() => {
    if (embedded || !debugBrowserSessionId) {
      return;
    }
    return () => useRecordingStore.getState().reset();
  }, [embedded, debugBrowserSessionId]);
  // Embedded: the shell owns the stream, so bind the copilot once the backend
  // session exists — else it gets a null id and the backend spins a separate browser.
  const copilotRequiresLiveBrowser =
    (showBrowser || embedded) && shouldFetchDebugSession && !isRateLimited;
  // readyBrowserSessionId is keyed to the browser session id rather than a
  // bare boolean: when activeDebugSession's id changes, stale ready state
  // from the previous session cannot leak into the next render.
  const copilotLiveBrowserReady = resolveCopilotLiveBrowserReady({
    displayReady: Boolean(
      readyBrowserSessionId && readyBrowserSessionId === debugBrowserSessionId,
    ),
    hasBackendSession: Boolean(debugBrowserSessionId),
    headlessTurnDrainEnabled: headlessTurnDrainEnabled || embedded,
  });
  const debugSessionExpiryWarningKeyRef = useRef<string | null>(null);

  const { data: liveBrowserSession, dataUpdatedAt: liveBrowserSessionNowMs } =
    useQuery<BrowserSessionData>({
      queryKey: ["browserSession", debugBrowserSessionId],
      queryFn: async () => {
        if (!debugBrowserSessionId) {
          throw new Error("Cannot fetch browser session without an ID");
        }
        const client = await getClient(credentialGetter, "sans-api-v1");
        const response = await client.get<BrowserSessionData>(
          `/browser_sessions/${debugBrowserSessionId}`,
        );
        return response.data;
      },
      enabled:
        Boolean(debugBrowserSessionId) &&
        shouldFetchDebugSession &&
        !isRateLimited,
      refetchInterval: DEBUG_SESSION_EXPIRY_STATUS_REFETCH_MS,
      refetchOnWindowFocus: true,
    });

  const handleLiveBrowserReadyChange = useCallback(
    (ready: boolean, sessionId: string | null) => {
      if (activeRunSessionIdRef.current !== null) {
        return;
      }
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
  // (e.g. /agents/A/build → /agents/B/build); in that case the
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
    // empty), unless the URL asks for a specific block; fires only on workflow
    // change, so tab-switch selection persists.
    const initialSelectedBlockId = getInitialSelectedBlockId({
      enabled: embedded,
      nodes: initialNodes,
      searchParams,
    });
    useWorkflowPanelStore.getState().setSelectedBlockId(initialSelectedBlockId);
    useShowAllCodeStore.getState().reset();
    useSidebarSaveStateStore.getState().reset();
    // Drop A's unsaved-changes baseline on an A→B same-instance route swap;
    // a carried snapshot would diff B's graph against A's and surface phantom
    // "edited" lines. clearSnapshot resets contentDirty + userHasEdited too.
    useWorkflowSnapshotStore.getState().clearSnapshot();
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
  useSelectedBlockUrlSync({
    enabled: embedded,
    nodes,
    getNodes: getNodes as () => Array<AppNode>,
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
      shouldPollDebugSessionInvalidation({
        debugSession,
        debugSessionError,
        shouldFetchDebugSession,
        workflowPermanentId,
        isRateLimited,
      })
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
    debugSessionError,
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

  useApplyRecordedBlocks({
    // Recording runs in the debugger (build + showBrowser), not only on /edit.
    enabled: !workflowPanelState.data?.showComparison,
    nodes,
    edges,
    doLayout,
  });

  // Studio entry point for recording: the Browser-tab Record button lives outside
  // the canvas, so the canvas-aware Workspace registers a launcher that resolves
  // the append-at-end insertion point (the trailing top-level NodeAdder, same as
  // clicking its "+") and starts recording. MVP only appends at the end.
  const setStartRecordingAtEnd = useRecordingLauncherStore(
    (s) => s.setStartRecordingAtEnd,
  );
  // Stable action ref (vs the whole-store `recordingStore` object, which gets a
  // new reference on every store write and would re-register the launcher).
  const setIsRecording = useRecordingStore((s) => s.setIsRecording);
  const setRecordedBlocks = useRecordedBlocksStore((s) => s.setRecordedBlocks);
  const getAppendInsertionPoint = useCallback(() => {
    return resolveAppendInsertionPoint(
      getNodes() as Array<AppNode>,
      getEdges(),
    );
  }, [getEdges, getNodes]);
  const sopToBlocksMutation = useSopToBlocksMutation({
    onSuccess: (result, owner) => {
      applySopResultAtCurrentAppend({
        result,
        getNodes: () => getNodes() as Array<AppNode>,
        getEdges,
        setRecordedBlocks: (blocks, insertionPoint) =>
          setRecordedBlocks(blocks, insertionPoint, owner),
      });
    },
  });
  const authoringActionAvailability =
    resolveWorkspaceAuthoringActionAvailability({
      browserReady: copilotLiveBrowserReady,
      isGlobalWorkflow,
      isWorkflowDeleted: Boolean(workflow.deleted_at),
      hasActiveRun: Boolean(viewerState?.active_run_session_id),
      isComparing: Boolean(workflowPanelState.data?.showComparison),
      isEditingYaml: yamlEditorActive,
      hasFinallyBlock: Boolean(finallyBlockLabel),
      isRecording:
        recordingStore.isRecording ||
        recordingStore.finishRequested ||
        recordingStore.isCommitting,
      isUploadingSOP: sopToBlocksMutation.isPending,
    });
  const [, setRecordSearchParams] = useSearchParams();
  const autoRecordRequested = searchParams.get("record") === "1";
  const authoringBlocked = useWorkflowYamlEditorStore(
    (state) =>
      state.commitInProgress ||
      state.copilotAcceptance !== null ||
      state.authoringInProgress,
  );
  const startRecordingAtEnd = useCallback(() => {
    if (!authoringActionAvailability.canRecordTask) return;
    void runWorkflowAuthoringAction(() => {
      const insertionPoint = getAppendInsertionPoint();
      setWorkflowPanelState({
        active: false,
        content: "nodeLibrary",
        data: {
          previous: insertionPoint.previous,
          next: insertionPoint.next,
          parent: undefined,
          connectingEdgeType: "default",
        },
      });
      setIsRecording(true, {
        workflowPermanentId: workflowPermanentId ?? null,
        browserSessionId: debugBrowserSessionId,
      });
      if (autoRecordRequested) {
        setRecordSearchParams(
          (current) => {
            const next = new URLSearchParams(current);
            next.delete("record");
            return next;
          },
          { replace: true },
        );
      }
    });
  }, [
    getAppendInsertionPoint,
    setWorkflowPanelState,
    setIsRecording,
    workflowPermanentId,
    debugBrowserSessionId,
    authoringActionAvailability.canRecordTask,
    autoRecordRequested,
    setRecordSearchParams,
  ]);
  const uploadSOPAtEnd = useCallback(
    (file: File) => {
      if (!authoringActionAvailability.canUploadSOP) return;
      void runWorkflowAuthoringAction(() =>
        sopToBlocksMutation.mutateAsync(file),
      );
    },
    [authoringActionAvailability.canUploadSOP, sopToBlocksMutation],
  );
  // `/discover`'s "Record task" lands here with ?record=1; start once the browser is ready.
  useEffect(() => {
    if (
      !autoRecordRequested ||
      !authoringActionAvailability.canRecordTask ||
      authoringBlocked
    ) {
      return;
    }
    startRecordingAtEnd();
  }, [
    autoRecordRequested,
    authoringActionAvailability.canRecordTask,
    authoringBlocked,
    startRecordingAtEnd,
  ]);
  useEffect(() => {
    if (!embedded) {
      return;
    }
    setStartRecordingAtEnd(
      authoringActionAvailability.canRecordTask ? startRecordingAtEnd : null,
    );
    return () => setStartRecordingAtEnd(null);
  }, [
    authoringActionAvailability.canRecordTask,
    embedded,
    startRecordingAtEnd,
    setStartRecordingAtEnd,
  ]);

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
    if (refuseMutationDuringYamlCommit()) return;
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

  const invalidateSavedWorkflow = () => {
    if (!workflowPermanentId) return;
    queryClient.invalidateQueries({
      queryKey: ["workflow", workflowPermanentId],
    });
    queryClient.invalidateQueries({ queryKey: ["workflows"] });
    queryClient.invalidateQueries({
      queryKey: ["block-scripts", workflowPermanentId],
    });
  };

  const applyWorkflowUpdate = (
    workflowData: WorkflowVersion,
    options?: {
      persisted?: boolean;
      keepLocalGraph?: boolean;
      userDriven?: boolean;
      midTurnDraft?: boolean;
      settings?: WorkflowSettings;
      metadataPatch?: MetadataPatch;
      fromYamlCommit?: boolean;
    },
  ): boolean => {
    if (!options?.fromYamlCommit && refuseMutationDuringYamlCommit())
      return false;
    if (!options?.fromYamlCommit) reconcileYamlDraftAfterGraphChange();
    useWorkflowYamlEditorStore.getState().bumpRevision();
    const settings = options?.settings ?? apiWorkflowToSettings(workflowData);

    const elements = getElements(
      workflowData.workflow_definition.blocks,
      settings,
      true,
    );

    const collapsedSet = useNodeCollapseStore.getState().collapsed;
    const wpid = workflowPermanentId ?? "__global__";
    if (!options?.keepLocalGraph) {
      setParameterBaselines((current) => ({
        ...current,
        [workflowData.workflow_permanent_id]:
          workflowData.workflow_definition.parameters,
      }));
      updateNodes(
        replayPersistedCollapseVisibility(elements.nodes, wpid, collapsedSet),
      );
      updateEdges(elements.edges);
      useWorkflowParametersStore
        .getState()
        .setParameters(getInitialParameters(workflowData), options);
      useWorkflowParametersStore.setState({
        parametersWorkflowPermanentId: workflowData.workflow_permanent_id,
      });
    }
    if (options?.persisted) setAcceptedWorkflow(workflowData);

    // Sync title so snap-back on Reject reverts the editor's title bar
    // alongside the canvas blocks. A mid-turn draft is not authoritative: it must
    // not clobber a rename made while the turn was running, and a draft still
    // carrying the placeholder must not mark the title as generated.
    if (options?.fromYamlCommit) {
      applyYamlCommitMetadata(
        workflowData,
        options.metadataPatch ?? {},
        options.persisted ?? false,
      );
    } else {
      const titleStore = useWorkflowTitleStore.getState();
      if (typeof workflowData.title === "string") {
        if (options?.midTurnDraft) {
          titleStore.setTitleFromCopilotIfDefault(workflowData.title);
        } else {
          titleStore.syncTitleFromWorkflow(workflowData.title, options);
        }
      }
      titleStore.setDescriptionFromWorkflow(workflowData.description, options);
    }

    if (options?.persisted) {
      useWorkflowTitleStore.setState({
        titleWorkflowPermanentId: workflowData.workflow_permanent_id,
        descriptionWorkflowPermanentId: workflowData.workflow_permanent_id,
      });
      // Atomic accept: server wrote a new version; treat as clean baseline and refresh cached workflow.
      workflowChangesStore.setHasChanges(
        options.keepLocalGraph ?? false,
        options,
      );
      if (options.keepLocalGraph) {
        const definition = convert(workflowData).workflow_definition;
        useWorkflowSnapshotStore.setState({
          snapshot: {
            blocks: definition.blocks,
            parameters: definition.parameters,
            settings: apiWorkflowToSettings(workflowData),
            title: workflowData.title,
            description: workflowData.description,
          },
          userHasEdited: true,
          contentDirty: true,
        });
      }
      invalidateSavedWorkflow();
    } else {
      workflowChangesStore.setHasChanges(true, options);
      if (options?.userDriven) {
        // A Copilot build has no canvas gesture but is user-driven; mark it so
        // the dot/summary surface it instead of the baseline absorbing it.
        useWorkflowSnapshotStore.getState().markUserEdit();
      }
    }
    return true;
  };

  const handleCopilotWorkflowUpdate = useWorkspaceCopilotUpdate({
    applyWorkflowUpdate,
  });

  const enterYamlMode = () => {
    if (isGlobalWorkflow) {
      return;
    }
    const saveData = workflowChangesStore.getSaveData?.();
    if (!saveData) {
      toast({
        title: "Cannot edit YAML",
        description: "The workflow is still loading. Try again in a moment.",
        variant: "destructive",
      });
      return;
    }
    try {
      const yaml = convertToYAML(
        buildWorkflowYamlDocument({
          ...saveData,
          definitionVersion: saveData.workflowDefinitionVersion,
        }),
      );
      yamlEntryHadChangesRef.current =
        useWorkflowHasChangesStore.getState().hasChanges;
      // Freeze the pre-edit canvas as the clean baseline before the draft can
      // diverge: a baseline captured once the draft is dirty is taken from the
      // draft itself (effectiveDraft prefers it), baking the uncommitted edit in.
      if (useWorkflowSnapshotStore.getState().snapshot === null) {
        useWorkflowSnapshotStore.getState().captureSnapshot();
      }
      useWorkflowYamlEditorStore.getState().open(yaml);
    } catch (error) {
      toast({
        title: "Cannot edit YAML",
        description:
          error instanceof Error
            ? error.message
            : "Failed to build Workflow YAML",
        variant: "destructive",
      });
    }
  };

  // Expose Code-mode entry via the store so header chrome outside this
  // closure (studio's Editor pane header, the legacy overflow menu's "View
  // schema" item) can enter Code mode without owning serialization. A stable
  // wrapper over a ref keeps the registration from churning while still
  // calling the latest closure.
  const enterYamlModeRef = useRef(enterYamlMode);
  enterYamlModeRef.current = enterYamlMode;
  useEffect(() => {
    if (isGlobalWorkflow) {
      return;
    }
    const store = useWorkflowYamlEditorStore.getState();
    store.registerEnterYamlMode(() => enterYamlModeRef.current());
    return () => store.registerEnterYamlMode(null);
  }, [embedded, isGlobalWorkflow]);

  // Commit-on-switch: reparse the edited YAML into the graph via the Copilot's
  // non-persisting convert endpoint. Returns false on invalid YAML (stays open).
  const commitYaml = async (
    persist: boolean = false,
    codeCacheDeletionApproved?: boolean,
  ): Promise<boolean> => {
    const yamlStore = useWorkflowYamlEditorStore.getState();
    const owner = yamlCommitOwnerRef.current;
    if (!owner || !beginYamlCommit(owner)) return false;
    const revision = yamlStore.revision;
    const yamlCommit = { owner, revision };
    try {
      if (isGlobalWorkflow) {
        yamlStore.close();
        return false;
      }
      const saveData = workflowChangesStore.getSaveData?.();
      if (
        !saveData ||
        saveData.workflow.workflow_permanent_id !== owner.workflowPermanentId
      )
        return false;
      if (!isWorkflowYamlDirty(yamlStore)) {
        let savedWorkflow: WorkflowVersion | undefined;
        if (persist) {
          const errors = getWorkflowErrors(nodes);
          if (errors.length > 0) {
            toast({
              title: "Can not save workflow because of errors:",
              description: errors.join(" "),
              variant: "destructive",
            });
            return false;
          }
          try {
            const saved = await persistYamlCommitIfCurrent(
              revision,
              () =>
                saveWorkflow.mutateAsync({
                  ...saveData,
                  yamlCommit,
                  codeCacheDeletionApproved,
                }),
              owner,
            );
            if (!saved || !saved.response) return false;
            savedWorkflow = saved.response.data;
          } catch {
            return false;
          }
        }
        if (
          !isYamlCommitOwnerCurrent(owner) ||
          !isYamlCommitRevisionCurrent(revision, persist)
        )
          return false;
        if (persist) {
          if (!savedWorkflow) return false;
          workflowChangesStore.hydrateSavedSettings?.(savedWorkflow);
          applyYamlCommitMetadata(savedWorkflow, {}, true);
          workflowChangesStore.setHasChanges(false, { fromYamlCommit: true });
          invalidateSavedWorkflow();
        }
        yamlStore.close();
        return true;
      }
      const parsed: {
        title?: unknown;
        description?: unknown;
        parameters?: WorkflowSaveData["parameters"];
        blocks?: WorkflowSaveData["blocks"];
      } = parseYAML(yamlStore.draft);
      const {
        definition: draftDefinition,
        definitionYaml,
        settingsPatch,
        metadataPatch,
      } = yamlCommitInputs(parsed, yamlStore.draft);
      const baseline = parseYAML(yamlStore.entrySnapshot) as {
        [key: string]: unknown;
        workflow_definition?: Record<string, unknown>;
      } | null;
      const baselineDefinition = baseline?.workflow_definition ?? baseline;
      const baselineSettings = {
        ...baseline,
        ...baseline?.workflow_definition,
      };
      const metadataEdits: MetadataPatch = {};
      if (metadataPatch.title !== undefined && parsed.title !== baseline?.title)
        metadataEdits.title = metadataPatch.title;
      if (
        Object.prototype.hasOwnProperty.call(metadataPatch, "description") &&
        parsed.description !== baseline?.description
      )
        metadataEdits.description = metadataPatch.description;
      const settings = applySettingsPatch(saveData.settings, settingsPatch);
      const mergedSaveData: WorkflowSaveData = {
        ...saveData,
        settings,
        title: metadataPatch.title?.trim() ?? saveData.title,
        description: Object.prototype.hasOwnProperty.call(
          metadataPatch,
          "description",
        )
          ? (metadataPatch.description ?? null)
          : saveData.description,
      };
      const headerDocument = buildWorkflowYamlDocument({
        ...mergedSaveData,
        definitionVersion: saveData.workflowDefinitionVersion,
      });
      const client = await getClient(credentialGetter, "sans-api-v1");
      if (!isYamlCommitOwnerCurrent(owner)) return false;
      const response = await client.post<WorkflowYAMLConversionResponse>(
        "/workflow/copilot/convert-yaml-to-blocks",
        {
          workflow_definition_yaml: definitionYaml,
          workflow_id: saveData.workflow.workflow_id,
        },
      );
      if (!isYamlCommitOwnerCurrent(owner)) return false;
      const { nodes: repairedNodes, edges: repairedEdges } = getElements(
        response.data.workflow_definition.blocks ?? [],
        settings,
        true,
      );
      const repairedBlocks = getWorkflowBlocks(repairedNodes, repairedEdges);
      settings.finallyBlockLabel = resolveFinallyBlockLabel(
        settings.finallyBlockLabel,
        settingsPatch,
        repairedBlocks,
      );
      const definition: WorkflowDefinition = {
        ...response.data.workflow_definition,
        finally_block_label: settings.finallyBlockLabel,
        workflow_system_prompt: settings.workflowSystemPrompt,
        error_code_mapping: settings.errorCodeMapping,
        retry_policy: settings.retryPolicy,
      };
      let version = workflowVersionFromSaveData(mergedSaveData, definition, {
        extraHttpHeaders: headerDocument.extra_http_headers ?? null,
        cdpConnectHeaders: headerDocument.cdp_connect_headers ?? null,
      });
      // The legacy version projection is also used elsewhere; the transaction
      // carries live metadata explicitly so a hydrate cannot replay stale values.
      version.description = mergedSaveData.description;
      if (persist) {
        const repairedErrors = getWorkflowErrors(repairedNodes);
        if (repairedErrors.length > 0) {
          toast({
            title: "Can not save workflow because of errors:",
            description: repairedErrors.join(" "),
            variant: "destructive",
          });
          return false;
        }
        const { blocks: upgradedBlocks, version: upgradedVersion } =
          upgradeWorkflowDefinitionToVersionTwo(
            repairedBlocks,
            response.data.workflow_definition.version ??
              saveData.workflowDefinitionVersion,
          );
        try {
          const saved = await persistYamlCommitIfCurrent(
            revision,
            () =>
              saveWorkflow.mutateAsync({
                ...mergedSaveData,
                blocks: upgradedBlocks,
                parameters: draftDefinition.parameters ?? [],
                workflowDefinitionVersion: upgradedVersion,
                yamlCommit,
                codeCacheDeletionApproved,
              }),
            owner,
          );
          if (!saved || !saved.response) return false;
          version = saved.response.data;
        } catch {
          // The mutation surfaces server errors; retain the draft for correction.
          return false;
        }
      } else if (!isYamlCommitRevisionCurrent(revision)) {
        return false;
      }
      if (
        !isYamlCommitOwnerCurrent(owner) ||
        !isYamlCommitRevisionCurrent(revision, persist)
      )
        return false;
      // A non-persisting commit is a user edit that lands async (the convert
      // round-trip outruns the canvas gesture window), so mark it — otherwise
      // the baseline absorbs it as post-load materialization and the save
      // confirmation sees nothing to confirm. A persisting commit already moved
      // the baseline the only other way it may move: a successful save.
      applyWorkflowUpdate(version, {
        persisted: persist,
        userDriven: !persist,
        settings: persist ? apiWorkflowToSettings(version) : settings,
        metadataPatch: metadataEdits,
        fromYamlCommit: true,
      });
      if (
        JSON.stringify(draftDefinition.blocks) !==
          JSON.stringify(baselineDefinition?.blocks) ||
        JSON.stringify(draftDefinition.parameters) !==
          JSON.stringify(baselineDefinition?.parameters) ||
        Object.entries(settingsPatch).some(
          ([key, value]) =>
            JSON.stringify(value) !== JSON.stringify(baselineSettings[key]),
        )
      )
        useWorkflowTitleStore.getState().recordCopilotGraphEdit();
      yamlStore.close();
      return true;
    } catch (error) {
      if (!isYamlCommitOwnerCurrent(owner)) return false;
      const detail =
        error instanceof AxiosError
          ? (error.response?.data?.detail ?? error.message)
          : error instanceof Error
            ? error.message
            : "Could not convert YAML into workflow blocks.";
      yamlStore.setError(detail);
      return false;
    } finally {
      finishYamlCommit(owner);
    }
  };

  useWorkflowYamlEditorLifecycle(commitYaml);

  // Reflect YAML-draft dirtiness in the unsaved-changes flag so the existing
  // tab-close / navigation guards protect the draft — two-way, so reverting the
  // draft restores the dirty state from when YAML mode opened.
  useEffect(() => {
    if (yamlEditorActive) {
      useWorkflowHasChangesStore
        .getState()
        .setHasChanges(yamlEntryHadChangesRef.current || yamlEditorDirty);
    }
  }, [yamlEditorActive, yamlEditorDirty]);

  // Studio exit from version history/comparison: keep the current version,
  // clear the comparison, close the panel. Panes stay editor-only.
  const exitVersionHistory = () => {
    setWorkflowPanelState({
      active: false,
      content: "history",
      data: {
        showComparison: false,
        version1: undefined,
        version2: undefined,
      },
    });
  };

  const handleSelectState = (selectedVersion: WorkflowVersion) => {
    if (refuseMutationDuringYamlCommit()) return;
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
    const settings = apiWorkflowToSettings(selectedVersion);

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
    const titleStore = useWorkflowTitleStore.getState();
    titleStore.clearCopilotMetadata(selectedVersion.workflow_permanent_id);
    titleStore.trackCopilotMetadata(
      selectedVersion.workflow_permanent_id,
      titleStore.copilotMetadataEdits[selectedVersion.workflow_permanent_id]
        ?.proposal,
    );
    titleStore.setTitle(selectedVersion.title, { source: "workflow" });
    titleStore.setDescriptionFromWorkflow(selectedVersion.description);
  };

  return (
    <div
      className="relative h-full w-full"
      style={
        {
          // Studio has no block-config settings sidebar (settings are inline in
          // the blocks), so zero the var; the on-canvas sidebar-offset consumers
          // are suppressed there anyway. Legacy keeps the overlay's measured width.
          [BLOCK_SIDEBAR_WIDTH_VAR]: embedded
            ? "0px"
            : `${renderedBlockSidebarWidth}px`,
        } as React.CSSProperties
      }
    >
      {!yamlEditorActive ? (
        <div className="absolute inset-x-0 top-0 z-50">
          <WorkflowSavePendingNotice />
        </div>
      ) : null}
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
              <div className="pb-2 pt-4 text-sm text-muted-foreground">
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
                await confirmCodeCacheDeletion(handleOnSave);
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
              onExit={embedded ? exitVersionHistory : undefined}
            />
          </div>
          {workflowPanelState.active &&
            workflowPanelState.content === "history" && (
              <div className="shrink-0">
                <WorkflowHistoryPanel
                  workflowPermanentId={workflowPermanentId!}
                  onCompare={handleCompareVersions}
                  onClose={embedded ? exitVersionHistory : undefined}
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
              onExit={embedded ? exitVersionHistory : undefined}
            />
          </div>

          {/* sub panels */}
          {workflowPanelState.active && (
            <div
              className={cn(
                "absolute z-30 transition-all duration-300 ease-out",
                embedded ? "top-3" : "top-[8.5rem]",
                // Studio: the settings panel is a separate grid column, so
                // in-stage sub-panels anchor to the Stage edge, never offset.
                !embedded && blockSidebarOpen
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
              {!embedded && workflowPanelState.content === "cacheKeyValues" && (
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
                    onClose={embedded ? exitVersionHistory : undefined}
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
            <div
              className="relative flex h-full w-full overflow-hidden overflow-x-hidden"
              // The YAML surface covers this subtree visually but it would stay
              // in the tab order; inert removes it (pane variant has no trap).
              {...(yamlEditorActive ? { inert: "" } : {})}
            >
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
                parameterBaseline={
                  parameterBaselines[workflow.workflow_permanent_id]
                }
                embedded={embedded}
                paneEntryKey={embedded ? studioEntryId : undefined}
                paneLayoutKey={
                  embedded
                    ? `${studioPanes.join(",")}|${paneWidthsKey(studioPaneWidths)}`
                    : undefined
                }
                onRequestDeleteNode={handleRequestDeleteNode}
                captureHistoryImmediately={captureWorkflowEditImmediately}
                onAddNode={addNode}
                historyApplyTrigger={historyApplyTrigger + restoreApplyTrigger}
              />

              {/* Studio hosts the toggle in the Editor pane header; legacy
                  anchors it under the header on /edit only (the debugger
                  mounts this Workspace too). */}
              {!yamlEditorActive &&
              !isGlobalWorkflow &&
              !embedded &&
              isEditRoute ? (
                <div
                  className={cn(
                    "absolute top-[8.5rem] z-30 transition-all duration-300 ease-out",
                    blockSidebarOpen
                      ? HEADER_RIGHT_INSET_OPEN
                      : HEADER_RIGHT_INSET_CLOSED,
                  )}
                  style={{
                    transform: headerCollapsed
                      ? "translateY(calc(-100% - 8.5rem))"
                      : "translateY(0)",
                    opacity: headerCollapsed ? 0 : 1,
                  }}
                >
                  <YamlModeToggle mode="visual" onCode={enterYamlMode} />
                </div>
              ) : null}

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
                      !embedded && blockSidebarOpen
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
                    {!embedded &&
                      workflowPanelState.content === "cacheKeyValues" && (
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
                          onClose={embedded ? exitVersionHistory : undefined}
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
              {!embedded && workflowPanelState.content === "cacheKeyValues" && (
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
                    onClose={embedded ? exitVersionHistory : undefined}
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
            {/* code + canvas; recording overlays the panel without unmounting FlowRenderer */}
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
                  recordingStore.isRecording && "pointer-events-none invisible",
                )}
                aria-hidden={recordingStore.isRecording}
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
                  className={cn("relative h-full w-[50%]", {
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
                    parameterBaseline={
                      parameterBaselines[workflow.workflow_permanent_id]
                    }
                    containerResizeTrigger={containerResizeTrigger}
                    onRequestDeleteNode={handleRequestDeleteNode}
                    captureHistoryImmediately={captureWorkflowEditImmediately}
                    onAddNode={addNode}
                    historyApplyTrigger={
                      historyApplyTrigger + restoreApplyTrigger
                    }
                    onLayoutPhaseChange={setFlowLayoutPhase}
                  />
                  {!blockLabel && (
                    <div className="pointer-events-none absolute bottom-3 left-1/2 z-20 w-[20rem] max-w-[calc(100%-2rem)] -translate-x-1/2 [&>*]:pointer-events-auto">
                      <RecentActivityRunSelector
                        contentSide="top"
                        contentAlign="center"
                      />
                    </div>
                  )}
                </div>
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

                {/* Live browser: mode comes from the session's stream transport, falling back to runtime config */}
                {showVncBrowserPanel && (
                  <div className="skyvern-vnc-browser flex h-full w-[calc(100%_-_6rem)] flex-1 flex-col items-center justify-center">
                    <div key={reloadKey} className="w-full flex-1">
                      {!displayBrowserSessionId ? (
                        isDebugSessionError ? (
                          debugSessionErrorPanel
                        ) : (
                          <StreamStatusPanel
                            diagnostic={{
                              title: "Starting browser session",
                              detail:
                                "Creating a debug browser session for this agent.",
                            }}
                          />
                        )
                      ) : isFlowCanvasReady || recordingStore.isRecording ? (
                        <BrowserStream
                          key={displayBrowserSessionId}
                          exfiltrate={
                            recordingStore.isRecording &&
                            !recordingStore.finishRequested
                          }
                          interactive={true}
                          browserSessionId={displayBrowserSessionId}
                          showControlButtons={true}
                          // The recording panel overlays the canvas whenever a
                          // recording is live here, so the REC pill is redundant.
                          hideRecordingIndicator={true}
                          // Remounts across the CDP<->VNC recording swap; the
                          // debug session owns the recording reset.
                          resetRecordingOnUnmount={false}
                          resizeTrigger={windowResizeTrigger}
                          isExecuting={
                            !!workflowRun && runIsExecuting(workflowRun)
                          }
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
                      {!displayBrowserSessionId ? (
                        isDebugSessionError ? (
                          debugSessionErrorPanel
                        ) : (
                          <StreamStatusPanel
                            diagnostic={{
                              title: "Starting browser session",
                              detail:
                                "Creating a debug browser session for this agent.",
                            }}
                          />
                        )
                      ) : isFlowCanvasReady || recordingStore.isRecording ? (
                        <BrowserSessionStream
                          browserSessionId={displayBrowserSessionId}
                          interactive={true}
                          showControlButtons={true}
                          // The CDP transport streams the page viewport only, so
                          // unlike the VNC panel there is no browser address bar
                          // to type into and a session resting on about:blank has
                          // no way out (SKY-13705).
                          enableUrlInput={true}
                          // undefined keeps the recording message channel closed on
                          // the non-recording live view; a defined value opens it.
                          exfiltrate={
                            recordingStore.isRecording
                              ? !recordingStore.finishRequested
                              : undefined
                          }
                          workflowPermanentId={workflowPermanentId ?? null}
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
                        "pointer-events-none absolute left-0 top-0 h-full w-[6rem] rounded-l-lg opacity-0 transition-all duration-1000 [transition-delay:300ms]",
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
        captureEditorState={captureLiveEditorState}
        restoreEditorState={restoreLiveEditorState}
        onWorkflowPersisted={(workflowPermanentId) =>
          useWorkflowHasChangesStore
            .getState()
            .recordPersistedSave(workflowPermanentId)
        }
        isOpen={embedded ? studioCopilotOpen : showBrowser && isCopilotOpen}
        docked={embedded}
        chromeless={embedded}
        portalTarget={embedded ? studioCopilotPortalEl : undefined}
        onClose={() => setIsCopilotOpen(false)}
        onMessageCountChange={setCopilotMessageCount}
        buttonRef={copilotButtonRef}
        liveBrowserSessionId={
          copilotLiveBrowserReady ? debugBrowserSessionId : null
        }
        onTurnActivityChange={setIsCopilotTurnActive}
        workflowRunId={copilotRunId({ embedded, studioRunId })}
        requiresLiveBrowser={copilotRequiresLiveBrowser}
        isLiveBrowserReady={copilotLiveBrowserReady}
        initialMessage={initialCopilotMessage ?? undefined}
        initialAttachments={routeInitialCopilotAttachments}
        initialAction={initialCopilotAction ?? undefined}
        onInitialMessageConsumed={handleInitialCopilotMessageConsumed}
        onUploadSOP={uploadSOPAtEnd}
        canUploadSOP={authoringActionAvailability.canUploadSOP}
        isUploadingSOP={sopToBlocksMutation.isPending}
        onRecordTask={startRecordingAtEnd}
        canRecordTask={authoringActionAvailability.canRecordTask}
        authoringUnavailableReason={
          authoringActionAvailability.unavailableReason
        }
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
        onReviewWorkflow={async (pendingWorkflow, clearPending, reject) => {
          const saveData = workflowChangesStore.getSaveData?.();
          if (!saveData) return;

          try {
            // Create YAML from current workflow definition only
            const workflowDefinitionYaml = convertToYAML({
              version: saveData.workflowDefinitionVersion,
              parameters: saveData.parameters,
              blocks: saveData.blocks,
              retry_policy: saveData.settings.retryPolicy ?? null,
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
                  description: `Invalid JSON format in extra http headers: ${getJsonParseErrorDetail(
                    saveData.settings.extraHttpHeaders ?? "",
                    error,
                  )}`,
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
                  description: `Invalid JSON format in cdp connect headers: ${getJsonParseErrorDetail(
                    saveData.settings.cdpConnectHeaders ?? "",
                    error,
                  )}`,
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
              description: saveData.description || null,
              workflow_definition:
                currentConversionResponse.data.workflow_definition,
              proxy_location: saveData.settings.proxyLocation,
              webhook_callback_url: saveData.settings.webhookCallbackUrl,
              extra_http_headers: extraHttpHeaders,
              cdp_connect_headers: cdpConnectHeaders,
              persist_browser_session: saveData.settings.persistBrowserSession,
              reuse_browser_session: saveData.settings.reuseBrowserSession,
              pin_saved_session_ip: saveData.settings.pinSavedSessionIp,
              browser_profile_id: saveData.settings.browserProfileId,
              browser_profile_key: saveData.settings.browserProfileKey,
              model: saveData.settings.model,
              totp_verification_url: saveData.settings.totpVerificationUrl,
              totp_identifier: saveData.settings.totpIdentifier,
              max_screenshot_scrolls: saveData.settings.maxScreenshotScrolls,
              max_elapsed_time_minutes:
                saveData.settings.maxElapsedTimeMinutes ?? null,
              status: saveData.workflow.status,
              created_at: new Date().toISOString(),
              modified_at: new Date().toISOString(),
              deleted_at: null,
              run_with: saveData.settings.runWith,
              browser_type: saveData.settings.browserType ?? null,
              cache_key: saveData.settings.scriptCacheKey,
              ai_fallback: saveData.settings.aiFallback,
              enable_self_healing: saveData.workflow.enable_self_healing,
              adaptive_caching: saveData.settings.adaptiveCaching,
              generate_script_on_terminal:
                saveData.settings.generateScriptOnTerminal,
              mask_secrets: saveData.settings.maskSecrets,
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

            const handleCopilotReviewClose = bindCopilotReviewClose(
              reject,
              async (status) => {
                if (status === "approve") {
                  try {
                    const restored = restoreWorkflowCopilotSettings(
                      pendingWorkflow,
                      workflowChangesStore.getSaveData?.()?.settings ??
                        saveData.settings,
                    );
                    if (
                      !applyWorkflowUpdate(pendingWorkflow, {
                        userDriven: true,
                        settings: restored.settings,
                      })
                    ) {
                      return;
                    }
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
                    return;
                  }
                }

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

                if (status === "approve") {
                  clearPending();
                }
              },
            );

            // Hide chat and show comparison. The comparison renders on the
            // editor canvas, so surface the Editor pane when docked in the studio.
            setIsCopilotOpen(false);
            if (embedded) {
              openStudioPane("editor");
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
        onWorkflowUpdate={handleCopilotWorkflowUpdate}
      />
      <ConfirmDialog
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
        title={`Delete block "${deleteBlockDialogState.nodeLabel}"?`}
        description="This block will be deleted from the agent."
        reversible
        onConfirm={() => {
          if (refuseMutationDuringYamlCommit()) return;
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
      >
        <AffectedBlocksNotice affectedBlocks={affectedBlocksForDelete} />
      </ConfirmDialog>

      {/* Studio: the cache key/value panel escapes the Editor pane via the
          shell-level portal, so the Overview pane's Code view can open it (and
          close it) even with the Editor pane hidden. */}
      {embedded ? (
        <StudioShellPanelPortal
          open={
            workflowPanelState.active &&
            workflowPanelState.content === "cacheKeyValues"
          }
          onDismiss={closeWorkflowPanel}
        >
          <WorkflowCacheKeyValuesPanel
            cacheKeyValues={cacheKeyValues}
            pending={cacheKeyValuesLoading}
            scriptKey={workflow.cache_key ?? "default"}
            filter={cacheKeyValueFilter ?? undefined}
            onFilterChange={setCacheKeyValueFilter}
            onClose={closeWorkflowPanel}
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
        </StudioShellPanelPortal>
      ) : null}
      {/* Studio: Code mode swaps the Editor pane's content (sibling panes stay
          usable); legacy keeps the original full-screen modal overlay. */}
      {yamlEditorActive ? (
        <WorkflowYamlEditor
          workflowId={workflow.workflow_permanent_id}
          variant={embedded ? "pane" : "fullscreen"}
        />
      ) : null}
    </div>
  );
}

export { CopyText, CopyAndExplainCode, Workspace };
