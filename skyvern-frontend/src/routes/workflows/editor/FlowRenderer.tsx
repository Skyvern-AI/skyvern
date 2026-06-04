import { usePostHog } from "posthog-js/react";
import { LogoMinimized } from "@/components/LogoMinimized";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useOnChange } from "@/hooks/useOnChange";
import { cn } from "@/util/utils";
import { useShouldNotifyWhenClosingTab } from "@/hooks/useShouldNotifyWhenClosingTab";
import { BlockActionContext } from "@/store/BlockActionContext";
import { useDebugStore } from "@/store/useDebugStore";
import { useRecordedBlocksStore } from "@/store/RecordedBlocksStore";
import {
  useWorkflowHasChangesStore,
  useWorkflowSave,
  type WorkflowSaveData,
} from "@/store/WorkflowHasChangesStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { useWorkflowSettingsStore } from "@/store/WorkflowSettingsStore";
import { useWorkflowTitleStore } from "@/store/WorkflowTitleStore";
import { ReloadIcon } from "@radix-ui/react-icons";
import {
  DndContext,
  DragOverlay,
  closestCenter,
  type DragEndEvent,
  type DragOverEvent,
  type DragStartEvent,
} from "@dnd-kit/core";
import {
  Background,
  BackgroundVariant,
  Controls,
  Edge,
  PanOnScrollMode,
  ReactFlow,
  Viewport,
  useNodesInitialized,
  useReactFlow,
  NodeChange,
  EdgeChange,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { nanoid } from "nanoid";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useDebouncedCallback } from "use-debounce";
import { useBlocker, useParams } from "react-router-dom";
import {
  AWSSecretParameter,
  debuggableWorkflowBlockTypes,
  WorkflowApiResponse,
  WorkflowEditorParameterTypes,
  WorkflowParameterTypes,
  WorkflowParameterValueType,
} from "../types/workflowTypes";
import {
  BitwardenCreditCardDataParameterYAML,
  BitwardenLoginCredentialParameterYAML,
  BitwardenSensitiveInformationParameterYAML,
  ContextParameterYAML,
  CredentialParameterYAML,
  OnePasswordCredentialParameterYAML,
  AzureVaultCredentialParameterYAML,
  ParameterYAML,
  WorkflowParameterYAML,
} from "../types/workflowYamlTypes";
import {
  BITWARDEN_CLIENT_ID_AWS_SECRET_KEY,
  BITWARDEN_CLIENT_SECRET_AWS_SECRET_KEY,
  BITWARDEN_MASTER_PASSWORD_AWS_SECRET_KEY,
} from "./constants";
import { edgeTypes } from "./edges";
import {
  AppNode,
  isWorkflowBlockNode,
  nodeTypes,
  WorkflowBlockNode,
} from "./nodes";
import { GlobalCollapseControl } from "./collapse/GlobalCollapseControl";
import { WorkflowScopeContext } from "./WorkflowScopeContext";
import { FitViewControl } from "./controls/FitViewControl";
import { RedoControl } from "./controls/RedoControl";
import { ToggleInteractivityControl } from "./controls/ToggleInteractivityControl";
import { UndoControl } from "./controls/UndoControl";
import { ZoomInControl } from "./controls/ZoomInControl";
import { ZoomOutControl } from "./controls/ZoomOutControl";
import { blockTypeFromNode } from "./nodes/blockTypeFromNode";
import {
  ParametersState,
  parameterIsSkyvernCredential,
  parameterIsOnePasswordCredential,
  parameterIsBitwardenCredential,
  parameterIsAzureVaultCredential,
} from "./types";
import "./reactFlowOverrideStyles.css";
import {
  convertEchoParameters,
  convertToNode,
  createNode,
  descendants,
  generateNodeLabel,
  getAdditionalParametersForEmailBlock,
  getOrderedChildrenBlocks,
  getOutputParameterKey,
  getWorkflowBlocks,
  getWorkflowSettings,
  layout,
  removeJinjaReferenceFromNodes,
  removeKeyFromNodesParameterKeys,
  upgradeWorkflowDefinitionToVersionTwo,
  getWorkflowErrors,
} from "./workflowEditorUtils";
import { toast } from "@/components/ui/use-toast";
import { useAutoPan } from "./useAutoPan";
import { useAutoGenerateWorkflowTitle } from "../hooks/useAutoGenerateWorkflowTitle";
import { SortableBlockScope } from "./sortable/SortableBlockScope";
import {
  TOP_LEVEL_SCOPE,
  collectConditionalBranchScopes,
  collectLoopScopes,
  getOrderedBlockIdsAtScope,
  getScopeKey,
  type SortableBlockScopeDescriptor,
} from "./sortable/scope";
import { classifyBlockDrop } from "./sortable/rewire";
import { findForwardReferenceViolations } from "./sortable/forwardRefs";
import { findFinallyBlockNodeId } from "./sortable/finallyBlockGate";
import { useDndDragActivityStore } from "./sortable/dndDragActivity";
import {
  isDragGatedByMode,
  type DragModeGateInputs,
} from "./sortable/dragModeGate";
import { showDropBlockedToast } from "./sortable/dropBlockedToast";
import { useDragSensors } from "./sortable/dragSensors";
import { createScopeAwareKeyboardCoordinates } from "./sortable/scopeAwareKeyboardCoordinates";
import { DropPositionIndicator } from "./sortable/DropPositionIndicator";
import {
  deriveDropIndicator,
  type DropIndicatorState,
} from "./sortable/dropIndicator";
import {
  SCREEN_READER_INSTRUCTIONS,
  buildDragAnnouncements,
} from "./sortable/dragAnnouncements";
import { PoliteDndLiveRegionPolicy } from "./sortable/dragLiveRegionPolicy";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useIsCanvasLocked } from "./controls/useIsCanvasLocked";
import { BlockConfigSidebar } from "./panels/BlockConfigSidebar";
import { BlockSidebarMigrationPopover } from "./panels/BlockSidebarMigrationPopover";

// Grace period after nodesInitialized before we start tracking changes.
// Allows mount-time effects (ResizeObserver, visibility toggling) to settle.
// Async API calls (e.g. WorkflowTriggerNode title hydration) are separately
// protected by beginInternalUpdate/endInternalUpdate guards, so this timeout
// only needs to cover synchronous mount-time effects.
const INITIAL_LOAD_SETTLE_MS = 500;

function convertToParametersYAML(
  parameters: ParametersState,
): Array<
  | WorkflowParameterYAML
  | BitwardenLoginCredentialParameterYAML
  | ContextParameterYAML
  | BitwardenSensitiveInformationParameterYAML
  | BitwardenCreditCardDataParameterYAML
  | OnePasswordCredentialParameterYAML
  | AzureVaultCredentialParameterYAML
  | CredentialParameterYAML
> {
  return parameters
    .map(
      (
        parameter: ParametersState[number],
      ):
        | WorkflowParameterYAML
        | BitwardenLoginCredentialParameterYAML
        | ContextParameterYAML
        | BitwardenSensitiveInformationParameterYAML
        | BitwardenCreditCardDataParameterYAML
        | OnePasswordCredentialParameterYAML
        | AzureVaultCredentialParameterYAML
        | CredentialParameterYAML
        | undefined => {
        if (parameter.parameterType === WorkflowEditorParameterTypes.Workflow) {
          // Convert boolean default values to strings for backend
          let defaultValue = parameter.defaultValue;
          if (
            parameter.dataType === "boolean" &&
            typeof parameter.defaultValue === "boolean"
          ) {
            defaultValue = String(parameter.defaultValue);
          }
          if (
            (parameter.dataType === "integer" ||
              parameter.dataType === "float") &&
            (typeof parameter.defaultValue === "number" ||
              typeof parameter.defaultValue === "string")
          ) {
            defaultValue =
              parameter.defaultValue === null
                ? parameter.defaultValue
                : String(parameter.defaultValue);
          }

          return {
            parameter_type: WorkflowParameterTypes.Workflow,
            key: parameter.key,
            description: parameter.description || null,
            workflow_parameter_type: parameter.dataType,
            ...(parameter.defaultValue === null
              ? {}
              : { default_value: defaultValue }),
          };
        } else if (
          parameter.parameterType === WorkflowEditorParameterTypes.Context
        ) {
          return {
            parameter_type: WorkflowParameterTypes.Context,
            key: parameter.key,
            description: parameter.description || null,
            source_parameter_key: parameter.sourceParameterKey,
          };
        } else if (
          parameter.parameterType === WorkflowEditorParameterTypes.Secret
        ) {
          return {
            parameter_type:
              WorkflowParameterTypes.Bitwarden_Sensitive_Information,
            key: parameter.key,
            bitwarden_identity_key: parameter.identityKey,
            bitwarden_identity_fields: parameter.identityFields,
            description: parameter.description || null,
            bitwarden_collection_id: parameter.collectionId,
            bitwarden_client_id_aws_secret_key:
              BITWARDEN_CLIENT_ID_AWS_SECRET_KEY,
            bitwarden_client_secret_aws_secret_key:
              BITWARDEN_CLIENT_SECRET_AWS_SECRET_KEY,
            bitwarden_master_password_aws_secret_key:
              BITWARDEN_MASTER_PASSWORD_AWS_SECRET_KEY,
          };
        } else if (
          parameter.parameterType ===
          WorkflowEditorParameterTypes.CreditCardData
        ) {
          return {
            parameter_type: WorkflowParameterTypes.Bitwarden_Credit_Card_Data,
            key: parameter.key,
            description: parameter.description || null,
            bitwarden_item_id: parameter.itemId,
            bitwarden_collection_id: parameter.collectionId,
            bitwarden_client_id_aws_secret_key:
              BITWARDEN_CLIENT_ID_AWS_SECRET_KEY,
            bitwarden_client_secret_aws_secret_key:
              BITWARDEN_CLIENT_SECRET_AWS_SECRET_KEY,
            bitwarden_master_password_aws_secret_key:
              BITWARDEN_MASTER_PASSWORD_AWS_SECRET_KEY,
          };
        } else {
          if (parameterIsBitwardenCredential(parameter)) {
            return {
              parameter_type: WorkflowParameterTypes.Bitwarden_Login_Credential,
              key: parameter.key,
              description: parameter.description || null,
              bitwarden_collection_id: parameter.collectionId,
              bitwarden_item_id: parameter.itemId,
              url_parameter_key: parameter.urlParameterKey,
              bitwarden_client_id_aws_secret_key:
                BITWARDEN_CLIENT_ID_AWS_SECRET_KEY,
              bitwarden_client_secret_aws_secret_key:
                BITWARDEN_CLIENT_SECRET_AWS_SECRET_KEY,
              bitwarden_master_password_aws_secret_key:
                BITWARDEN_MASTER_PASSWORD_AWS_SECRET_KEY,
            };
          } else if (parameterIsSkyvernCredential(parameter)) {
            return {
              parameter_type: WorkflowParameterTypes.Workflow,
              workflow_parameter_type: WorkflowParameterValueType.CredentialId,
              default_value: parameter.credentialId,
              key: parameter.key,
              description: parameter.description || null,
            };
          } else if (parameterIsOnePasswordCredential(parameter)) {
            return {
              parameter_type: WorkflowParameterTypes.OnePassword,
              key: parameter.key,
              description: parameter.description || null,
              vault_id: parameter.vaultId,
              item_id: parameter.itemId,
            };
          } else if (parameterIsAzureVaultCredential(parameter)) {
            return {
              parameter_type: WorkflowParameterTypes.Azure_Vault_Credential,
              key: parameter.key,
              description: parameter.description || null,
              vault_name: parameter.vaultName,
              username_key: parameter.usernameKey,
              password_key: parameter.passwordKey,
              totp_secret_key: parameter.totpSecretKey,
            };
          }
        }
        return undefined;
      },
    )
    .filter(
      (
        param:
          | WorkflowParameterYAML
          | BitwardenLoginCredentialParameterYAML
          | ContextParameterYAML
          | BitwardenSensitiveInformationParameterYAML
          | BitwardenCreditCardDataParameterYAML
          | OnePasswordCredentialParameterYAML
          | AzureVaultCredentialParameterYAML
          | CredentialParameterYAML
          | undefined,
      ): param is
        | WorkflowParameterYAML
        | BitwardenLoginCredentialParameterYAML
        | ContextParameterYAML
        | BitwardenSensitiveInformationParameterYAML
        | BitwardenCreditCardDataParameterYAML
        | OnePasswordCredentialParameterYAML
        | AzureVaultCredentialParameterYAML
        | CredentialParameterYAML => param !== undefined,
    );
}

type Props = {
  hideBackground?: boolean;
  showZoomControls?: boolean;
  // Render the canvas as a read-only viewer (no controls, no
  // selection-driven sidebar). Used by WorkflowComparisonPanel for the
  // history/copilot diff canvas.
  readOnly?: boolean;
  nodes: Array<AppNode>;
  edges: Array<Edge>;
  setNodes: (nodes: Array<AppNode>) => void;
  setEdges: (edges: Array<Edge>) => void;
  onNodesChange: (changes: Array<NodeChange<AppNode>>) => void;
  onEdgesChange: (changes: Array<EdgeChange>) => void;
  initialTitle: string;
  workflow: WorkflowApiResponse;
  onDebuggableBlockCountChange?: (count: number) => void;
  onMouseDownCapture?: () => void;
  zIndex?: number;
  // Counter that the parent bumps to force a re-layout (e.g. on container
  // resize). Treated as a trigger, not an event handler.
  containerResizeTrigger?: number;
  // Counter bumped by `useWorkflowHistory.applySnapshot` on every undo/redo.
  // Forces a fresh `doLayout` pass against the restored nodes/edges so
  // expand/collapse state doesn't land at positions cached from the prior
  // container layout (snapshot strips `measured`, so the dimension-change
  // re-layout path can race the new render).
  historyApplyTrigger?: number;
  onRequestDeleteNode?: (
    nodeId: string,
    nodeLabel: string,
    confirmCallback: () => void,
  ) => void;
  // Supplied by Workspace: invoke from within the same event
  // handler as an atomic composite mutation so it lands as a single undo
  // step. The canvas-only consumers that use FlowRenderer without the
  // history hook (e.g. WorkflowComparisonPanel) leave this undefined and
  // the reorder path no-ops the capture call.
  captureHistoryImmediately?: () => void;
  // Supplied by Workspace so the docked Block Library inside
  // BlockConfigSidebar can append a node. Read-only canvases
  // (comparison) leave this undefined.
  onAddNode?: (props: import("./Workspace").AddNodeProps) => void;
  // Fires on every layout-phase transition. Workspace uses this to gate
  // expensive sibling mounts (VNC stream, copilot transcripts) on the
  // canvas being settled, so the initial Dagre + fade-in pass owns the
  // main thread without competing work.
  onLayoutPhaseChange?: (
    phase: "pre-layout" | "initial-load" | "ready",
  ) => void;
};

function FlowRenderer({
  hideBackground = false,
  showZoomControls = true,
  readOnly = false,
  nodes,
  edges,
  setNodes,
  setEdges,
  onNodesChange,
  onEdgesChange,
  initialTitle,
  workflow,
  onDebuggableBlockCountChange,
  onMouseDownCapture,
  zIndex,
  containerResizeTrigger,
  historyApplyTrigger,
  onRequestDeleteNode,
  captureHistoryImmediately,
  onAddNode,
  onLayoutPhaseChange,
}: Props) {
  const { blockLabel: targettedBlockLabel } = useParams();
  const reactFlowInstance = useReactFlow();
  const postHog = usePostHog();
  const debugStore = useDebugStore();
  const recordingStore = useRecordingStore();
  const isCanvasLocked = useIsCanvasLocked();
  const { title, initializeTitle } = useWorkflowTitleStore();
  const parameters = useWorkflowParametersStore((state) => state.parameters);
  const finallyBlockLabel = useWorkflowSettingsStore(
    (state) => state.finallyBlockLabel,
  );
  const nodesInitialized = useNodesInitialized();
  const [shouldConstrainPan, setShouldConstrainPan] = useState(false);
  const setSelectedBlockId = useWorkflowPanelStore(
    (state) => state.setSelectedBlockId,
  );
  const selectedBlockId = useWorkflowPanelStore(
    (state) => state.selectedBlockId,
  );

  // Escape clears the canvas selection. The listener is mounted
  // on the FlowRenderer because that scopes the global keydown to the
  // editor view; the WorkflowEditor route is the only mount point for it.
  // Read-only canvases (WorkflowComparisonPanel) skip the listener
  // entirely so an Escape press to dismiss compare/copilot UI cannot
  // mutate the main editor's shared selection store.
  useEffect(() => {
    if (readOnly) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") {
        return;
      }
      setSelectedBlockId(null);
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [setSelectedBlockId, readOnly]);

  // Programmatic viewport changes (e.g. `fitView`) animate via `setViewport`,
  // which fires `onMove`. Without this gate, `constrainPan` would clamp every
  // animation frame back to the lock and cancel the in-flight fit. Set
  // before invoking the API and cleared after the animation duration.
  const fitViewInProgressRef = useRef(false);

  const runFitView = useCallback(
    (options?: { maxZoom?: number; duration?: number }) => {
      const duration = options?.duration ?? 300;
      fitViewInProgressRef.current = true;
      reactFlowInstance.fitView({
        maxZoom: options?.maxZoom ?? 1,
        duration,
      });
      // Small safety buffer so a frame near the tail of the animation cannot
      // re-enter `constrainPan` before the final viewport lands.
      window.setTimeout(() => {
        fitViewInProgressRef.current = false;
      }, duration + 50);
    },
    [reactFlowInstance],
  );

  // Keep a ref so the keyboard handler can pick up the latest closure without
  // adding `runFitView` to its useEffect deps (which would re-bind the global
  // listener on every reactFlowInstance identity change).
  const runFitViewRef = useRef(runFitView);
  runFitViewRef.current = runFitView;

  // Canvas zoom + fit-view keyboard shortcuts. Gated to non-read-only
  // canvases for the same reason as the Escape handler above. Skip when
  // the user is typing in any text field so Cmd+- in a textarea still
  // moves the caret instead of zooming the canvas.
  useEffect(() => {
    if (readOnly) return;
    const isInsideEditableField = (target: EventTarget | null): boolean => {
      if (!(target instanceof HTMLElement)) return false;
      const tag = target.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
        return true;
      }
      return target.isContentEditable;
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (isInsideEditableField(event.target)) return;
      const meta = event.metaKey || event.ctrlKey;
      if (event.shiftKey && (event.key === "!" || event.key === "1")) {
        event.preventDefault();
        runFitViewRef.current?.();
        return;
      }
      if (meta && (event.key === "=" || event.key === "+")) {
        event.preventDefault();
        reactFlowInstance.zoomIn({ duration: 200 });
        return;
      }
      if (meta && (event.key === "-" || event.key === "_")) {
        event.preventDefault();
        reactFlowInstance.zoomOut({ duration: 200 });
        return;
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [reactFlowInstance, readOnly]);

  // Keep React Flow's internal `selected` flag in lockstep with the
  // store; downstream RF features (delete-key, multi-select) read it.
  // The updater returns `current` unchanged when no node flips, so RF
  // skips a full canvas re-render on the typical two-node delta.
  useEffect(() => {
    reactFlowInstance.setNodes((current) => {
      let changed = false;
      const next = current.map((node) => {
        const shouldBeSelected = node.id === selectedBlockId;
        if (Boolean(node.selected) === shouldBeSelected) {
          return node;
        }
        changed = true;
        return { ...node, selected: shouldBeSelected };
      });
      return changed ? next : current;
    });
  }, [selectedBlockId, reactFlowInstance]);

  // Track layout phase for animation control:
  // "pre-layout" = nodes hidden while Dagre hasn't computed positions yet
  // "initial-load" = nodes fading in at their final positions (no position transition)
  // "ready" = normal operation with position transitions enabled
  // The 350ms timeout is coupled with the CSS fade-in duration (300ms) in reactFlowOverrideStyles.css
  const [layoutPhase, setLayoutPhase] = useState<
    "pre-layout" | "initial-load" | "ready"
  >("pre-layout");
  // Mirror layoutPhase out to any opt-in parent (e.g. Workspace) without
  // adding it to the deps of every nearby effect. Fires on each transition
  // so consumers can flip "ready" mounts in one place.
  useEffect(() => {
    onLayoutPhaseChange?.(layoutPhase);
  }, [layoutPhase, onLayoutPhaseChange]);
  const hasCompletedInitialLoad = useRef(false);
  const fadeTimerRef = useRef<ReturnType<typeof setTimeout>>();
  const flowIsConstrained = debugStore.isDebugMode;

  // Track if this is the initial load to prevent false "unsaved changes" detection
  const isInitialLoadRef = useRef(true);

  // Track if we're currently in a layout operation to prevent infinite loops
  const isLayoutingRef = useRef(false);

  useEffect(() => {
    if (nodesInitialized) {
      setShouldConstrainPan(true);
      // Delay marking initial load as complete to allow mount-time effects
      // (ResizeObserver, async data fetches, visibility toggling) to settle
      // before we start tracking changes. Must be long enough for async
      // effects but short enough that users won't notice.
      const timer = setTimeout(() => {
        isInitialLoadRef.current = false;
      }, INITIAL_LOAD_SETTLE_MS);
      return () => clearTimeout(timer);
    }
  }, [nodesInitialized]);

  useEffect(() => {
    // The title store is shared with the editor view's save data builder.
    // In read-only / comparison renders the title from a historical version
    // would otherwise overwrite the live editor title and the next user
    // save would persist that stale comparison title.
    if (readOnly) return;
    initializeTitle(initialTitle);
  }, [initialTitle, initializeTitle, readOnly]);

  const workflowChangesStore = useWorkflowHasChangesStore();
  const setGetSaveDataRef = useRef(workflowChangesStore.setGetSaveData);
  setGetSaveDataRef.current = workflowChangesStore.setGetSaveData;
  const saveWorkflow = useWorkflowSave({ status: "published" });
  const recordedBlocks = useRecordedBlocksStore((state) => state.blocks);
  const recordedParameters = useRecordedBlocksStore(
    (state) => state.parameters,
  );
  const recordedInsertionPoint = useRecordedBlocksStore(
    (state) => state.insertionPoint,
  );
  const clearRecordedBlocks = useRecordedBlocksStore(
    (state) => state.clearRecordedBlocks,
  );
  useShouldNotifyWhenClosingTab(!readOnly && workflowChangesStore.hasChanges);
  const blocker = useBlocker(({ currentLocation, nextLocation }) => {
    return (
      !readOnly &&
      workflowChangesStore.hasChanges &&
      nextLocation.pathname !== currentLocation.pathname
    );
  });
  const blockerRef = useRef(blocker);
  blockerRef.current = blocker;

  const doLayout = useCallback(
    (nodes: Array<AppNode>, edges: Array<Edge>) => {
      const layoutedElements = layout(nodes, edges, targettedBlockLabel);
      setNodes(layoutedElements.nodes);
      setEdges(layoutedElements.edges);
    },
    [setNodes, setEdges, targettedBlockLabel],
  );

  // Debounced layout for dimension changes to prevent infinite loops (React error #185)
  // when copy-pasting triggers rapid successive dimension changes
  const debouncedLayoutForDimensions = useDebouncedCallback(
    (tempNodes: Array<AppNode>, currentEdges: Array<Edge>) => {
      if (isLayoutingRef.current) {
        return;
      }
      isLayoutingRef.current = true;
      try {
        doLayout(tempNodes, currentEdges);
      } finally {
        // Reset the flag after a short delay to allow React to flush updates
        requestAnimationFrame(() => {
          isLayoutingRef.current = false;
        });
      }
    },
    50,
    { leading: true, trailing: true, maxWait: 200 },
  );

  useEffect(() => {
    if (nodesInitialized && !hasCompletedInitialLoad.current) {
      hasCompletedInitialLoad.current = true;
      doLayout(nodes, edges);
      // After Dagre computes positions, wait one frame for the DOM to update
      // with new positions, then fade in the nodes/edges at their final positions.
      const rafId = requestAnimationFrame(() => {
        setLayoutPhase("initial-load");
        // After the fade-in animation completes, enable normal transform transitions
        fadeTimerRef.current = setTimeout(() => {
          setLayoutPhase("ready");
        }, 350);
      });
      return () => {
        cancelAnimationFrame(rafId);
        if (fadeTimerRef.current) clearTimeout(fadeTimerRef.current);
      };
    }
    // Initial-load layout fires once when nodesInitialized flips true;
    // re-running on every nodes/edges/doLayout change would re-trigger the
    // pre-layout fade after every edit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodesInitialized]);

  // Re-layout when the targetted block changes to account for the status row
  // that appears when a block is being debugged
  useEffect(() => {
    if (nodesInitialized && targettedBlockLabel) {
      doLayout(nodes, edges);
    }
    // Re-layout only when the targetted (debugged) block label changes.
    // nodes/edges/doLayout intentionally omitted: a normal edit already
    // triggers its own layout pass, and re-running here would fight the
    // user's interactive position changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [targettedBlockLabel]);

  // Re-layout when a loop node's header height changes (e.g., data schema toggled)
  useEffect(() => {
    const timerRef: { current: ReturnType<typeof setTimeout> | null } = {
      current: null,
    };
    const handleLoopHeaderResized = () => {
      // Delay to let React process the updateNodeData state change
      if (timerRef.current !== null) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => {
        timerRef.current = null;
        const currentNodes = reactFlowInstance.getNodes() as Array<AppNode>;
        const currentEdges = reactFlowInstance.getEdges();
        debouncedLayoutForDimensions(currentNodes, currentEdges);
      }, 10);
    };

    window.addEventListener("loop-header-resized", handleLoopHeaderResized);
    return () => {
      window.removeEventListener(
        "loop-header-resized",
        handleLoopHeaderResized,
      );
      if (timerRef.current !== null) clearTimeout(timerRef.current);
    };
  }, [reactFlowInstance, debouncedLayoutForDimensions]);

  // Re-layout when a conditional node's header height changes (e.g., expression textarea resized)
  useEffect(() => {
    const timerRef: { current: ReturnType<typeof setTimeout> | null } = {
      current: null,
    };
    const handleConditionalHeaderResized = () => {
      // Delay to let React process the updateNodeData state change
      if (timerRef.current !== null) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => {
        timerRef.current = null;
        const currentNodes = reactFlowInstance.getNodes() as Array<AppNode>;
        const currentEdges = reactFlowInstance.getEdges();
        debouncedLayoutForDimensions(currentNodes, currentEdges);
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
      if (timerRef.current !== null) clearTimeout(timerRef.current);
    };
  }, [reactFlowInstance, debouncedLayoutForDimensions]);

  // Re-layout when a workflow trigger node's async content changes
  // (e.g., target workflow parameters finish loading, skeleton → actual fields)
  useEffect(() => {
    const timerRef: { current: ReturnType<typeof setTimeout> | null } = {
      current: null,
    };
    const handleTriggerContentChanged = () => {
      if (timerRef.current !== null) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => {
        timerRef.current = null;
        const currentNodes = reactFlowInstance.getNodes() as Array<AppNode>;
        const currentEdges = reactFlowInstance.getEdges();
        debouncedLayoutForDimensions(currentNodes, currentEdges);
      }, 10);
    };

    window.addEventListener(
      "workflow-trigger-content-changed",
      handleTriggerContentChanged,
    );
    return () => {
      window.removeEventListener(
        "workflow-trigger-content-changed",
        handleTriggerContentChanged,
      );
      if (timerRef.current !== null) clearTimeout(timerRef.current);
    };
  }, [reactFlowInstance, debouncedLayoutForDimensions]);

  useEffect(() => {
    const topLevelBlocks = getWorkflowBlocks(nodes, edges);
    const debuggable = topLevelBlocks.filter((block) =>
      debuggableWorkflowBlockTypes.has(block.block_type),
    );

    for (const node of nodes) {
      const childBlocks = getOrderedChildrenBlocks(nodes, edges, node.id);

      for (const child of childBlocks) {
        if (debuggableWorkflowBlockTypes.has(child.block_type)) {
          debuggable.push(child);
        }
      }
    }

    onDebuggableBlockCountChange?.(debuggable.length);
  }, [nodes, edges, onDebuggableBlockCountChange]);

  const constructSaveData = useCallback((): WorkflowSaveData => {
    const blocks = getWorkflowBlocks(nodes, edges);
    const { blocks: upgradedBlocks, version: workflowDefinitionVersion } =
      upgradeWorkflowDefinitionToVersionTwo(
        blocks,
        workflow.workflow_definition.version,
      );
    const settings = getWorkflowSettings(nodes);
    const parametersInYAMLConvertibleJSON = convertToParametersYAML(parameters);
    const filteredParameters = workflow.workflow_definition.parameters.filter(
      (parameter) => {
        return parameter.parameter_type === "aws_secret";
      },
    ) as Array<AWSSecretParameter>;

    const echoParameters = convertEchoParameters(filteredParameters);

    const overallParameters = [
      ...parameters,
      ...echoParameters,
    ] as Array<ParameterYAML>;

    // if there is an email node, we need to add the email aws secret parameters
    const emailAwsSecretParameters = getAdditionalParametersForEmailBlock(
      upgradedBlocks,
      overallParameters,
    );

    return {
      parameters: [
        ...echoParameters,
        ...parametersInYAMLConvertibleJSON,
        ...emailAwsSecretParameters,
      ],
      blocks: upgradedBlocks,
      workflowDefinitionVersion,
      title,
      settings,
      workflow,
    };
  }, [nodes, edges, parameters, title, workflow]);

  useEffect(() => {
    if (readOnly) {
      // Comparison canvases mount FlowRenderer twice and must not stomp the
      // editor's getSaveData callback that header-level save/navigation-save
      // actions read from.
      return;
    }
    setGetSaveDataRef.current(constructSaveData);
  }, [constructSaveData, readOnly]);

  async function handleSave(): Promise<boolean> {
    // Validate before saving; block if any workflow errors exist
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
      return false;
    }
    await saveWorkflow.mutateAsync();
    return true;
  }

  const deleteNode = useCallback(
    (id: string) => {
      const node = nodes.find((node) => node.id === id);
      if (!node || !isWorkflowBlockNode(node)) {
        return;
      }
      const nodesToDelete = descendants(nodes, id);
      const deletedNodeLabel = node.data.label;
      const newNodes = nodes.filter(
        (node) => !nodesToDelete.includes(node) && node.id !== id,
      );
      const newEdges = edges.flatMap((edge) => {
        if (edge.source === id) {
          return [];
        }
        if (
          nodesToDelete.some(
            (node) => node.id === edge.source || node.id === edge.target,
          )
        ) {
          return [];
        }
        if (edge.target === id) {
          const nextEdge = edges.find((edge) => edge.source === id);
          if (nextEdge) {
            // connect the old incoming edge to the next node if both of them exist
            // also take the type of the old edge for plus button edge vs default
            return [
              {
                ...edge,
                type: nextEdge.type,
                target: nextEdge.target,
              },
            ];
          }
          return [edge];
        }
        return [edge];
      });

      if (newNodes.every((node) => node.type === "nodeAdder")) {
        // No user created nodes left, so return to the empty state.
        doLayout([], []);
        return;
      }

      // Step 1: Remove inline {{ deleted_block_output }} references from all nodes
      const deletedOutputKey = getOutputParameterKey(deletedNodeLabel);
      const nodesWithRemovedInlineRefs = removeJinjaReferenceFromNodes(
        newNodes,
        deletedOutputKey,
      );

      // Step 2: Remove from parameterKeys arrays and handle special cases
      const newNodesWithUpdatedParameters = removeKeyFromNodesParameterKeys(
        nodesWithRemovedInlineRefs,
        deletedOutputKey,
        deletedNodeLabel,
      );

      workflowChangesStore.setHasChanges(true);
      postHog.capture("builder.block.removed", {
        org_id: workflow.organization_id,
        block_type: blockTypeFromNode(node) ?? node.type,
      });

      // Clear the sidebar selection if the deleted block (or any of its
      // descendants) was the currently-selected one. Without this, the
      // sidebar keeps rendering against a stale id and `getNode` returns
      // undefined, leaving an empty/invalid inspector until the user
      // manually dismisses it.
      const deletedIds = new Set<string>([
        id,
        ...nodesToDelete.map((n) => n.id),
      ]);
      const currentSelected = useWorkflowPanelStore.getState().selectedBlockId;
      if (currentSelected !== null && deletedIds.has(currentSelected)) {
        useWorkflowPanelStore.getState().setSelectedBlockId(null);
      }

      doLayout(newNodesWithUpdatedParameters, newEdges);
    },
    [
      nodes,
      edges,
      doLayout,
      workflowChangesStore,
      postHog,
      workflow.organization_id,
    ],
  );

  // Use a ref to always have access to the latest deleteNode without causing re-renders
  const deleteNodeRef = useRef(deleteNode);
  useEffect(() => {
    deleteNodeRef.current = deleteNode;
  }, [deleteNode]);

  // Callback for requesting node deletion (opens confirmation dialog in parent)
  // Uses ref to avoid recreating on every nodes/edges change while still using latest deleteNode
  const requestDeleteNode = useCallback(
    (id: string, label: string) => {
      // Read-only canvases (WorkflowComparisonPanel) mount FlowRenderer
      // without `onRequestDeleteNode`. Without this gate the fallback
      // would mutate the graph and flip `hasChanges` while the user is
      // only reviewing versions.
      if (readOnly) return;
      if (onRequestDeleteNode) {
        onRequestDeleteNode(id, label, () => deleteNodeRef.current(id));
      } else {
        // Fallback: delete directly if no confirmation handler provided
        deleteNodeRef.current(id);
      }
    },
    [onRequestDeleteNode, readOnly],
  );

  function transmuteNode(id: string, nodeType: string) {
    const nodeToTransmute = nodes.find((node) => node.id === id);

    if (!nodeToTransmute || !isWorkflowBlockNode(nodeToTransmute)) {
      return;
    }

    const newNode = createNode(
      { id: nodeToTransmute.id, parentId: nodeToTransmute.parentId },
      nodeType as NonNullable<WorkflowBlockNode["type"]>,
      nodeToTransmute.data.label,
    );

    const newNodes = nodes.map((node) => {
      if (node.id === id) {
        return newNode;
      }
      return node;
    });

    workflowChangesStore.setHasChanges(true);
    doLayout(newNodes, edges);
  }

  function toggleScript({
    id,
    label,
    show,
  }: {
    id?: string;
    label?: string;
    show: boolean;
  }) {
    if (id) {
      const node = nodes.find((node) => node.id === id);
      if (!node) {
        return;
      }

      node.data.showCode = show;
    } else if (label) {
      const node = nodes.find(
        (node) => "label" in node.data && node.data.label === label,
      );

      if (!node) {
        return;
      }

      node.data.showCode = show;
    }

    doLayout(nodes, edges);
  }

  // effect to add new blocks that were generated from a browser recording,
  // along with any new parameters
  useEffect(() => {
    if (readOnly) {
      return;
    }
    if (!recordedBlocks || !recordedInsertionPoint) {
      return;
    }

    const { previous, next, parent, connectingEdgeType } =
      recordedInsertionPoint;

    const newNodes: Array<AppNode> = [];
    const newEdges: Array<Edge> = [];

    let existingLabels = nodes
      .filter(isWorkflowBlockNode)
      .map((node) => node.data.label);

    let prevNodeId = previous;

    // convert each WorkflowBlock to an AppNode
    recordedBlocks.forEach((block, index) => {
      const id = nanoid();
      const label = generateNodeLabel(existingLabels);
      existingLabels = [...existingLabels, label];
      const blockWithLabel = { ...block, label: block.label || label };

      const node = convertToNode(
        { id, parentId: parent },
        blockWithLabel,
        true,
      );
      newNodes.push(node);

      // create edge from previous node to this one
      if (prevNodeId) {
        newEdges.push({
          id: nanoid(),
          type: "edgeWithAddButton",
          source: prevNodeId,
          target: id,
          style: { strokeWidth: 2 },
        });
      }

      // if this is the last block, connect to next
      if (index === recordedBlocks.length - 1 && next) {
        newEdges.push({
          id: nanoid(),
          type: connectingEdgeType,
          source: id,
          target: next,
          style: { strokeWidth: 2 },
        });
      }

      prevNodeId = id;
    });

    const editedEdges = previous
      ? edges.filter((edge) => edge.source !== previous)
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
    doLayout(newNodesAfter, [...editedEdges, ...newEdges]);

    const newParameters = Array<ParametersState[number]>();

    for (const newParameter of recordedParameters ?? []) {
      const exists = parameters.some((param) => param.key === newParameter.key);

      if (!exists) {
        newParameters.push({
          key: newParameter.key,
          parameterType: "workflow",
          dataType: newParameter.workflow_parameter_type,
          description: newParameter.description ?? null,
          defaultValue: newParameter.default_value ?? "",
        });
      }
    }

    if (newParameters.length > 0) {
      const workflowParametersStore = useWorkflowParametersStore.getState();
      workflowParametersStore.setParameters([
        ...workflowParametersStore.parameters,
        ...newParameters,
      ]);
    }

    clearRecordedBlocks();
    // Effect runs strictly when the recording store flips a new
    // (blocks, insertionPoint) pair into place. nodes/edges/parameters etc.
    // are read from the latest closure inside the effect body; including
    // them in deps would re-fire on every editor edit and re-apply the
    // recorded blocks.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recordedBlocks, recordedInsertionPoint]);

  const editorElementRef = useRef<HTMLDivElement>(null);

  // Ordered ids of the top-level sortable siblings. M2 extends this with a
  // scope per loop container and per conditional branch
  // so a drag inside a nested container only reorders that
  // container's siblings.
  const topLevelSortableIds = useMemo(
    () => getOrderedBlockIdsAtScope({ nodes, edges, scope: TOP_LEVEL_SCOPE }),
    [nodes, edges],
  );

  // One scope per loop container. Each loop scope resolves its own
  // parentId-owned `__start_block__` head and `NodeAdderNode` tail, so its
  // ordered sibling ids exclude the head/tail and list only the loop's
  // direct children. Nested loops each get their own entry.
  const loopScopes: Array<{
    scope: SortableBlockScopeDescriptor;
    items: Array<string>;
  }> = useMemo(() => {
    return collectLoopScopes(nodes).map((scope) => ({
      scope,
      items: getOrderedBlockIdsAtScope({ nodes, edges, scope }),
    }));
  }, [nodes, edges]);

  // One scope per (conditionalNode, branch). A conditional shares a single
  // START and a single NodeAdder across all its branches, but each branch's
  // chain is fanned out via branch-tagged edges. Mounting one
  // SortableContext per branch keeps drops confined: branch A's sortable ids
  // are the branch A sibling chain, so a drag that originates in branch A
  // will only match an over id from branch A during hit testing.
  const conditionalBranchScopes: Array<{
    scope: SortableBlockScopeDescriptor;
    items: Array<string>;
  }> = useMemo(() => {
    return collectConditionalBranchScopes(nodes, edges).map((scope) => ({
      scope,
      items: getOrderedBlockIdsAtScope({ nodes, edges, scope }),
    }));
  }, [nodes, edges]);

  // Pre-computed `scopeKey → ordered ids` so onDndDragOver can derive the
  // drop indicator from cached order without re-walking the edge list on
  // every pointer event during a drag.
  const orderedIdsByScopeKey = useMemo(() => {
    const map = new Map<string, Array<string>>();
    map.set(getScopeKey(TOP_LEVEL_SCOPE), topLevelSortableIds);
    for (const { scope, items } of loopScopes) {
      map.set(getScopeKey(scope), items);
    }
    for (const { scope, items } of conditionalBranchScopes) {
      map.set(getScopeKey(scope), items);
    }
    return map;
  }, [topLevelSortableIds, loopScopes, conditionalBranchScopes]);

  // Resolve the scope that owns a given node id. A node whose parent is a
  // loop belongs to that loop's scope; a node whose parent is a conditional
  // belongs to its branch-specific scope (read from the node's
  // data.conditionalBranchId). Everything else falls through to
  // TOP_LEVEL_SCOPE.
  const scopeForActiveId = useCallback(
    (activeId: string): SortableBlockScopeDescriptor => {
      const activeNode = nodes.find((node) => node.id === activeId);
      const parentId = activeNode?.parentId ?? null;
      if (parentId === null) return TOP_LEVEL_SCOPE;
      const parentNode = nodes.find((node) => node.id === parentId);
      if (parentNode?.type === "loop") {
        return { parentId, conditionalBranchId: null };
      }
      if (parentNode?.type === "conditional") {
        // A conditional child without a branch id is a malformed node (it
        // would not be visible under any branch). Fall back to the top-level
        // scope so the drop is rejected rather than routed to an arbitrary
        // branch.
        if (
          activeNode &&
          isWorkflowBlockNode(activeNode) &&
          activeNode.data.conditionalBranchId
        ) {
          return {
            parentId,
            conditionalBranchId: activeNode.data.conditionalBranchId,
          };
        }
        return TOP_LEVEL_SCOPE;
      }
      return TOP_LEVEL_SCOPE;
    },
    [nodes],
  );

  // Scope-aware collision detection. The nested `SortableBlockScope`
  // mounts are siblings, not ancestors, of the per-node `useSortable`
  // calls in `withSortableBlock`, so dnd-kit binds every draggable to
  // the nearest ancestor `SortableContext` — the top-level scope.
  // Without filtering, `closestCenter` would report a top-level sibling
  // as the `over` candidate even when the user is dragging a loop /
  // branch child, and `classifyBlockDrop` would reject the drop as
  // cross-scope. Filter `droppableContainers` to the active block's
  // scope first so `closestCenter` only considers in-scope siblings.
  const collisionDetection = useCallback(
    (args: Parameters<typeof closestCenter>[0]) => {
      const activeId = String(args.active.id);
      const activeScopeKey = getScopeKey(scopeForActiveId(activeId));
      const filtered = args.droppableContainers.filter((container) => {
        return (
          getScopeKey(scopeForActiveId(String(container.id))) === activeScopeKey
        );
      });
      return closestCenter({ ...args, droppableContainers: filtered });
    },
    [scopeForActiveId],
  );

  // Pointer + keyboard sensors live together in `useDragSensors` so the
  // DndContext accepts both mouse/touch drags (5 px activation threshold
  // keeps clicks on the grip handle from immediately initiating a drag)
  // and keyboard-driven reorders routed through `sortableKeyboardCoordinates`.
  // The keyboard getter is wrapped with scope-aware filtering so arrow-key
  // reorders inside a loop/conditional only consider in-scope siblings,
  // matching the pointer-path `collisionDetection` above. The getter reads
  // through a ref so the underlying scope resolver can change with `nodes`
  // without forcing dnd-kit to re-instantiate sensors mid-drag.
  const scopeForActiveIdRef = useRef(scopeForActiveId);
  scopeForActiveIdRef.current = scopeForActiveId;
  const scopeAwareKeyboardCoordinates = useMemo(
    () =>
      createScopeAwareKeyboardCoordinates((id: string) =>
        getScopeKey(scopeForActiveIdRef.current(id)),
      ),
    [],
  );
  const dndSensors = useDragSensors(scopeAwareKeyboardCoordinates);

  // Replace dnd-kit's default id-based announcements with label-driven
  // messages so VoiceOver / NVDA users hear which block they picked up /
  // moved / dropped. Memoised on `nodes` so DndContext receives the same
  // object reference between renders that don't change the label set; the
  // dependency on `nodes` still guarantees the post-reorder labels are
  // visible by the time onDragEnd fires.
  const dndAnnouncements = useMemo(
    () => buildDragAnnouncements(nodes),
    [nodes],
  );

  // Commit a reorder drop in a single atomic mutation so the chain never
  // renders in an intermediate invalid state. Mirrors the rewire pattern
  // from deleteNode: drop the block out of its old slot and splice it into
  // the destination slot, then hand the new edges to doLayout alongside the
  // untouched nodes. The scope is resolved from the active block's parentId
  // so a drag inside a loop only touches that loop's sibling chain.
  //
  // Every refusal path routes through `showDropBlockedToast` so
  // users see a consistent, accessible message that names the specific
  // constraint — forward-reference, finally-pin, cross-scope, or a debug/
  // recording mode gate — instead of a silent no-op.
  // Track the currently-dragging node id so the DragOverlay can
  // render a label ghost following the pointer. Cleared at the start of
  // every outcome path in `onDndDragEnd` and on `onDragCancel` so the ghost
  // never outlives the gesture (including refused drops).
  const [activeDragId, setActiveDragId] = useState<string | null>(null);
  // Insertion-line indicator state. Derived per drag-over event
  // from the active + over ids via `deriveDropIndicator` and rendered by
  // `<DropPositionIndicator />` as a sibling of `<ReactFlow>`. Cross-scope
  // hovers return null from the helper, which matches the existing
  // classifier's cross-scope refusal.
  const [dropIndicator, setDropIndicator] = useState<DropIndicatorState>(null);
  // Mirror the local active drag id to a shared store so useWorkflowHistory
  // (mounted in Workspace, outside the FlowRenderer subtree) can gate
  // undo/redo on dnd-kit drag activity, not just RF node.dragging.
  const setSharedDndActiveDragId = useDndDragActivityStore(
    (s) => s.setActiveDragId,
  );
  const onDndDragStart = useCallback(
    (event: DragStartEvent) => {
      const id = String(event.active.id);
      setActiveDragId(id);
      setSharedDndActiveDragId(id);
    },
    [setSharedDndActiveDragId],
  );
  const onDndDragCancel = useCallback(() => {
    setActiveDragId(null);
    setSharedDndActiveDragId(null);
    setDropIndicator(null);
  }, [setSharedDndActiveDragId]);
  // If the editor unmounts mid-drag (route change, view swap), dnd-kit's
  // end/cancel handlers may never fire and the shared activity flag would
  // stay set, permanently disabling undo/redo via isDndDragInFlight().
  useEffect(() => {
    return () => {
      setSharedDndActiveDragId(null);
    };
  }, [setSharedDndActiveDragId]);
  const onDndDragOver = useCallback(
    (event: DragOverEvent) => {
      const { active, over } = event;
      if (!over) {
        setDropIndicator(null);
        return;
      }
      const activeId = String(active.id);
      const overId = String(over.id);
      const scope = scopeForActiveId(activeId);
      const order = orderedIdsByScopeKey.get(getScopeKey(scope)) ?? [];
      setDropIndicator(deriveDropIndicator({ order, activeId, overId }));
    },
    [orderedIdsByScopeKey, scopeForActiveId],
  );

  const onDndDragEnd = useCallback(
    (event: DragEndEvent) => {
      setActiveDragId(null);
      setSharedDndActiveDragId(null);
      setDropIndicator(null);
      // Read-only canvases (WorkflowComparisonPanel) must not commit
      // reorders, capture history, or mark the workflow dirty even if a
      // drag somehow reaches here (keyboard/programmatic).
      if (readOnly) return;
      const { active, over } = event;
      if (!over) return;
      const activeId = String(active.id);
      const overId = String(over.id);
      if (activeId === overId) return;

      const activeNode = nodes.find((node) => node.id === activeId);
      // Fall back to the node id so the toast still has a recognizable
      // token when a label is missing — shouldn't happen in practice
      // because labels are required, but better than an empty quoted
      // string in the description.
      const movedBlockLabel =
        typeof activeNode?.data?.label === "string" &&
        activeNode.data.label.length > 0
          ? activeNode.data.label
          : activeId;

      // Drag-mode gate: grip handle is already disabled at the DOM level when
      // recording or when the canvas is locked, but keyboard/programmatic
      // drags reach here.
      const dragModeGateInputs: DragModeGateInputs = {
        isRecording: recordingStore.isRecording,
        isCanvasLocked,
      };
      if (isDragGatedByMode(dragModeGateInputs)) {
        showDropBlockedToast({ kind: "drag-mode" });
        return;
      }

      const scope = scopeForActiveId(activeId);
      // Resolve the finally-block node id (if configured) so the rewire
      // helper can refuse drops that would displace it from the tail of
      // the top-level chain — extending the same !parentId gate used by
      // NodeAdderNode to disable appending past the finally block
      const finallyBlockId = findFinallyBlockNodeId(nodes, finallyBlockLabel);
      const outcome = classifyBlockDrop({
        nodes,
        edges,
        scope,
        activeId,
        overId,
        finallyBlockId,
      });
      if (outcome.kind === "noop") return;
      if (outcome.kind === "blocked") {
        if (outcome.reason === "finally-pin") {
          showDropBlockedToast({
            kind: "finally-pin",
            // `finallyBlockLabel` is non-null here because the classifier
            // only returns `finally-pin` when the caller supplied a
            // matching node id — which requires the label to be set.
            finallyBlockLabel: finallyBlockLabel ?? "",
          });
        } else if (outcome.reason === "chain-mismatch") {
          showDropBlockedToast({ kind: "chain-mismatch" });
        } else {
          showDropBlockedToast({
            kind: "cross-scope",
            movedBlockLabel,
          });
        }
        return;
      }

      // Block drops that would create a forward reference (a block that
      // contains {{movedBlock}} and now precedes the moved block). Skyvern
      // evaluates blocks in chain order, so unresolved forward refs would
      // silently null out at execution time.
      const violations = findForwardReferenceViolations({
        nodes,
        newOrder: outcome.newOrder,
        movedNodeId: activeId,
      });
      if (violations.length > 0) {
        showDropBlockedToast({
          kind: "forward-reference",
          movedBlockLabel,
          referrerLabels: violations.map((v) => v.referrerLabel),
        });
        return;
      }

      // Ask the history hook to treat the upcoming atomic mutation as a
      // single user-edit entry. Called BEFORE doLayout so the
      // post-commit capture effect sees the flag already set and the
      // rewire-plus-layout lands as one undo step instead of a debounced
      // tail that could either merge with a later edit or never fire if
      // the user promptly navigates away.
      captureHistoryImmediately?.();
      doLayout(nodes, outcome.edges);
      workflowChangesStore.setHasChanges(true);
    },
    [
      nodes,
      edges,
      doLayout,
      workflowChangesStore,
      scopeForActiveId,
      finallyBlockLabel,
      recordingStore.isRecording,
      isCanvasLocked,
      captureHistoryImmediately,
      readOnly,
      setSharedDndActiveDragId,
    ],
  );

  useAutoPan(editorElementRef, nodes);
  useAutoGenerateWorkflowTitle(nodes, edges, readOnly);

  useEffect(() => {
    doLayout(nodes, edges);
    // Trigger re-layout only when the parent container resizes. nodes/edges
    // mutations have their own layout paths and including them here would
    // double-layout after every edit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [containerResizeTrigger]);

  // Re-layout after every undo/redo. The history hook strips runtime
  // `measured`/`width`/`height` from snapshots so React Flow re-measures
  // after restore — but the dimension-change re-layout path can race the
  // new render (notably when undoing across a loop/conditional expand
  // toggle), leaving children at coordinates from the prior container
  // layout. Wait one frame for the restored nodes to commit + measure,
  // then re-run Dagre against the fresh sizes.
  useEffect(() => {
    if (!historyApplyTrigger) return;
    const rafId = requestAnimationFrame(() => {
      const currentNodes = reactFlowInstance.getNodes() as Array<AppNode>;
      const currentEdges = reactFlowInstance.getEdges();
      doLayout(currentNodes, currentEdges);
    });
    return () => cancelAnimationFrame(rafId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [historyApplyTrigger]);

  const zoomLock = 1 as const;
  const yLockMax = 140 as const;

  /**
   * Locks the x position of the flow to an ideal x based on the ideal width
   * of the flow. The ideal width is derived from the widest block type in
   * the canvas (loop / conditional / http_request) so the chain stays
   * visually centred regardless of which block types are present.
   */
  const getXLock = () => {
    const rect = editorElementRef.current?.getBoundingClientRect();

    if (!rect) {
      return 24;
    }

    const width = rect.width;
    const hasLoopBlock = nodes.some((node) => node.type === "loop");
    const hasHttpBlock = nodes.some((node) => node.type === "http_request");
    // Magic widths must be kept in sync with the actual rendered block
    // widths; introducing a wider block type without updating this
    // ladder will silently mis-centre the canvas.
    const idealWidth = hasHttpBlock ? 580 : hasLoopBlock ? 498 : 475;
    const split = (width - idealWidth) / 2;

    return Math.max(24, split);
  };

  useOnChange(debugStore.isDebugMode, (newValue) => {
    const xLock = getXLock();

    if (newValue) {
      const currentY = reactFlowInstance.getViewport().y;
      reactFlowInstance.setViewport({ x: xLock, y: currentY, zoom: zoomLock });
    }
  });

  const constrainPan = (viewport: Viewport) => {
    if (fitViewInProgressRef.current) return;
    const y = viewport.y;
    const yLockMin = nodes.reduce(
      (acc, node) => {
        const nodeBottom = node.position.y + (node.height ?? 0);
        if (nodeBottom > acc.value) {
          return { value: nodeBottom };
        }
        return acc;
      },
      { value: -Infinity },
    );
    const yLockMinValue = yLockMin.value;
    const xLock = getXLock();
    const newY = Math.max(-yLockMinValue + yLockMax, Math.min(yLockMax, y));

    // avoid infinite recursion with onMove
    if (
      viewport.x !== xLock ||
      viewport.y !== newY ||
      viewport.zoom !== zoomLock
    ) {
      reactFlowInstance.setViewport({
        x: xLock,
        y: newY,
        zoom: zoomLock,
      });
    }
  };

  // Memoize the context value so every FlowRenderer re-render doesn't
  // allocate a fresh object and re-fire every `useWorkflowScopeId` /
  // `useWorkflowScopeReadOnly` consumer (one per block node, plus the
  // collapse-store hook).
  const workflowScopeValue = useMemo(
    () => ({
      workflowId: workflow.workflow_permanent_id ?? null,
      readOnly,
    }),
    [workflow.workflow_permanent_id, readOnly],
  );

  return (
    <WorkflowScopeContext.Provider value={workflowScopeValue}>
      <div
        className={cn("workflow-editor-shell relative h-full w-full", {
          "react-flow--pre-layout": layoutPhase === "pre-layout",
          "react-flow--initial-load":
            layoutPhase === "initial-load" || layoutPhase === "pre-layout",
        })}
        style={{ zIndex }}
        onMouseDownCapture={() => onMouseDownCapture?.()}
      >
        {layoutPhase === "pre-layout" && (
          <div className="absolute inset-0 z-50 flex items-center justify-center bg-slate-950">
            <div className="animate-pulse">
              <LogoMinimized />
            </div>
          </div>
        )}
        <Dialog
          open={blocker.state === "blocked"}
          onOpenChange={(open) => {
            if (!open) {
              const current = blockerRef.current;
              if (current.state === "blocked") {
                current.reset?.();
              }
            }
          }}
        >
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Unsaved Changes</DialogTitle>
              <DialogDescription>
                Your workflow has unsaved changes. Do you want to save them
                before leaving?
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button
                variant="secondary"
                onClick={() => {
                  const current = blockerRef.current;
                  if (current.state === "blocked") {
                    current.proceed?.();
                  }
                }}
              >
                Continue without saving
              </Button>
              <Button
                onClick={() => {
                  handleSave().then((ok) => {
                    const current = blockerRef.current;
                    if (ok && current.state === "blocked") {
                      current.proceed?.();
                    }
                  });
                }}
                disabled={workflowChangesStore.saveIsPending}
              >
                {workflowChangesStore.saveIsPending && (
                  <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
                )}
                Save changes
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
        <BlockActionContext.Provider
          value={{
            // Read-only canvases (WorkflowComparisonPanel) must not expose
            // mutation callbacks. NodeHeader's transmute / delete /
            // script-toggle menus would otherwise still drive real edits
            // and flip `setHasChanges(true)` on the main editor's store
            // while the user is only inspecting versions.
            requestDeleteNodeCallback: readOnly ? () => {} : requestDeleteNode,
            // setTimeout(..., 0) escapes the Radix dropdown's pointer-event
            // lockout: a synchronous mutation inside the menu's onSelect
            // races the menu's close animation and re-renders nodes while
            // Radix still owns focus, swallowing the click.
            transmuteNodeCallback: readOnly
              ? () => {}
              : (id: string, nodeName: string) =>
                  setTimeout(() => transmuteNode(id, nodeName), 0),
            toggleScriptForNodeCallback: readOnly ? () => {} : toggleScript,
          }}
        >
          <DndContext
            sensors={dndSensors}
            collisionDetection={collisionDetection}
            onDragStart={onDndDragStart}
            onDragOver={onDndDragOver}
            onDragEnd={onDndDragEnd}
            onDragCancel={onDndDragCancel}
            accessibility={{
              announcements: dndAnnouncements,
              screenReaderInstructions: SCREEN_READER_INSTRUCTIONS,
            }}
          >
            <PoliteDndLiveRegionPolicy />
            <SortableBlockScope
              scope={TOP_LEVEL_SCOPE}
              items={topLevelSortableIds}
            >
              {loopScopes.map(({ scope, items }) => (
                <SortableBlockScope
                  key={scope.parentId ?? "__root__"}
                  scope={scope}
                  items={items}
                />
              ))}
              {conditionalBranchScopes.map(({ scope, items }) => (
                <SortableBlockScope
                  key={getScopeKey(scope)}
                  scope={scope}
                  items={items}
                />
              ))}
              <ReactFlow
                ref={editorElementRef}
                nodes={nodes}
                edges={edges}
                onNodesChange={(changes) => {
                  const dimensionChanges = changes.filter(
                    (change) => change.type === "dimensions",
                  );

                  // Only process dimension changes if we're not already in a layout operation
                  // This prevents infinite loops (React error #185) during copy-paste
                  if (dimensionChanges.length > 0 && !isLayoutingRef.current) {
                    const tempNodes = [...nodes];
                    let hasActualChanges = false;

                    dimensionChanges.forEach((change) => {
                      const node = tempNodes.find(
                        (node) => node.id === change.id,
                      );
                      if (node) {
                        const newWidth = change.dimensions?.width;
                        const newHeight = change.dimensions?.height;

                        // Only update if dimensions actually changed
                        if (
                          node.measured?.width !== newWidth ||
                          node.measured?.height !== newHeight
                        ) {
                          hasActualChanges = true;
                          node.measured = {
                            ...node.measured,
                            width: newWidth,
                            height: newHeight,
                          };
                        }
                      }
                    });

                    // Only trigger layout if there were actual dimension changes
                    if (hasActualChanges) {
                      debouncedLayoutForDimensions(tempNodes, edges);
                    }
                  }

                  // Only track changes after initial load is complete and not during internal updates
                  // (e.g., switching conditional branches which is UI state, not workflow data)
                  // Use getState() to get real-time value (not stale closure from render time)
                  const internalUpdateCount =
                    useWorkflowHasChangesStore.getState().internalUpdateCount;
                  if (
                    !readOnly &&
                    !isInitialLoadRef.current &&
                    internalUpdateCount === 0 &&
                    changes.some((change) => {
                      return (
                        change.type === "add" ||
                        change.type === "remove" ||
                        change.type === "replace" ||
                        // User drag-drop. `dragging === false` fires once at the
                        // end of a drag gesture. Programmatic position updates
                        // (mount-time layout, setNodes from node components)
                        // leave `dragging` undefined, so this filter doesn't
                        // falsely trip for them.
                        (change.type === "position" &&
                          change.dragging === false)
                      );
                    })
                  ) {
                    workflowChangesStore.setHasChanges(true);
                  }

                  onNodesChange(changes);
                }}
                onEdgesChange={onEdgesChange}
                onNodeClick={(_event, node) => {
                  if (readOnly) {
                    return;
                  }
                  // Allow workflow blocks AND the root Start node (workflow
                  // settings) to open the sidebar. Skip the NodeAdder
                  // ("+" button) and Start nodes nested inside loop /
                  // conditional containers - those are layout-only.
                  const appNode = node as AppNode;
                  const isWorkflowSettingsStart =
                    appNode.type === "start" &&
                    appNode.data.withWorkflowSettings;
                  if (
                    !isWorkflowBlockNode(appNode) &&
                    !isWorkflowSettingsStart
                  ) {
                    return;
                  }
                  setSelectedBlockId(node.id);
                }}
                onPaneClick={() => {
                  if (readOnly) {
                    return;
                  }
                  setSelectedBlockId(null);
                }}
                nodeTypes={nodeTypes}
                edgeTypes={edgeTypes}
                // colorMode="dark"
                fitView={true}
                fitViewOptions={{
                  maxZoom: 1,
                }}
                // Keep off-viewport workflow nodes mounted: dnd-kit sortable
                // drop targets register from mounted node components, so React
                // Flow virtualization would make long-chain reorder targets
                // disappear until scrolled into view.
                deleteKeyCode={null}
                onMove={(_, viewport) => {
                  if (flowIsConstrained && shouldConstrainPan) {
                    constrainPan(viewport);
                  }
                }}
                maxZoom={flowIsConstrained ? 1 : 2}
                minZoom={flowIsConstrained ? 1 : 0.5}
                panOnDrag={true}
                panOnScroll={true}
                panOnScrollMode={PanOnScrollMode.Vertical}
                zoomOnDoubleClick={!flowIsConstrained}
                zoomOnPinch={!flowIsConstrained}
                zoomOnScroll={!flowIsConstrained}
              >
                {!hideBackground && (
                  <Background
                    variant={BackgroundVariant.Dots}
                    bgColor="hsl(var(--background))"
                  />
                )}
                {!readOnly && (
                  <Controls
                    position="bottom-left"
                    showZoom={false}
                    showFitView={false}
                    showInteractive={false}
                  >
                    <UndoControl />
                    <RedoControl />
                    {showZoomControls && (
                      <>
                        <ZoomInControl />
                        <ZoomOutControl />
                      </>
                    )}
                    <FitViewControl onClick={() => runFitView()} />
                    <ToggleInteractivityControl />
                    <GlobalCollapseControl />
                  </Controls>
                )}
              </ReactFlow>
            </SortableBlockScope>
            {/*
            The overlay portals a label-only ghost out of the ReactFlow
            DOM so the user sees what they are dragging even as the
            original node fades to 40% in place. `dropAnimation={null}`
            keeps the drop instant: the atomic rewire in `onDndDragEnd`
            repositions the real node, and animating the ghost toward a
            stale location would flash a misaligned preview. The ghost
            is label-only (no full node render) because the real
            `.react-flow__node` DOM depends on RF's transform context
            that the overlay portal does not share.
          */}
            <DropPositionIndicator state={dropIndicator} />
            {/*
            Sidebar is rendered as a sibling of the ReactFlow canvas
            inside the FlowRenderer wrapper so it inherits the editor's
            height and React Flow context (the sidebar reads the
            selected node via useReactFlow). It mounts conditionally
            based on `selectedBlockId` and unmounts when null. Skipped
            entirely in read-only mode so comparison canvases never
            expose the editable block form.
          */}
            {!readOnly && <BlockConfigSidebar onAddNode={onAddNode} />}
            {!readOnly && <BlockSidebarMigrationPopover />}
            <DragOverlay dropAnimation={null}>
              {(() => {
                if (!activeDragId) return null;
                const activeDragNode = nodes.find((n) => n.id === activeDragId);
                const activeDragLabel =
                  typeof activeDragNode?.data?.label === "string"
                    ? activeDragNode.data.label
                    : null;
                if (!activeDragLabel) return null;
                return (
                  <div className="rounded border border-slate-500 bg-slate-elevation3 px-3 py-2 text-sm text-slate-100 opacity-90 shadow-lg">
                    {activeDragLabel}
                  </div>
                );
              })()}
            </DragOverlay>
          </DndContext>
        </BlockActionContext.Provider>
      </div>
    </WorkflowScopeContext.Provider>
  );
}

export { FlowRenderer, type Props as FlowRendererProps };
