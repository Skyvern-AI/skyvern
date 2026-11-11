import { graphNoteReadings } from "./noteSequences";
import type { WorkflowCopilotDemonstrationInsertion } from "../../copilot/workflowCopilotTypes";
import {
  type HTMLAttributes,
  type Ref,
  forwardRef,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { buildWorkflowSaveRequest } from "../workflowYamlDocument";
import type { Edge } from "@xyflow/react";
import { GearIcon, QuestionMarkCircledIcon } from "@radix-ui/react-icons";
import { usePostHog } from "posthog-js/react";
import { cn } from "@/util/utils";
import { useWorkflowPresentationStore } from "@/store/WorkflowPresentationStore";
import { useWorkflowTitleStore } from "@/store/WorkflowTitleStore";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { getClient } from "@/api/AxiosClient";
import { isAxiosError } from "axios";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import {
  selectEditorMutationLocked,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";
import {
  WorkflowEditorParameterTypes,
  WorkflowParameterValueType,
} from "../../types/workflowTypes";
import { isWorkflowBlockNode, type AppNode } from "../nodes";
import { useBrowserTypeOptionsQuery } from "@/routes/workflows/utils";
import { changedSettingCount } from "../nodes/StartNode/agentSettingsSummary";
import { useLocateBlockStore } from "../runValidation/useLocateBlockStore";
import { isEditableTarget } from "../editableTarget";
import {
  useWorkflowScopeId,
  useWorkflowScopeReadOnly,
} from "../WorkflowScopeContext";
import {
  buildDocTree,
  countDocSteps,
  docStepNumbers,
  type DocItem,
} from "./buildDocTree";
import { docLearnContent, type DocLearnContent } from "./docLearnContent";
import { docStarterDone, useDocStarter } from "./useDocStarter";
import { getDocInputChips, NOT_USED_TITLE } from "./docInputChips";
import {
  docInputNameProblem,
  isDocEditableInput,
  newDocInputKey,
  parseDocInputForm,
  removeDocInput,
  saveDocInput,
} from "./docInputs";
import { DocInputEditor } from "./DocInputEditor";
import {
  armNextCaretOffset,
  docStepLine,
  takeReferenceLine,
  useDocTouch,
} from "./docKeys";
import type { ParametersState } from "../types";
import { toast, useToast } from "@/components/ui/use-toast";
import {
  WorkflowDocActionsContext,
  type DocGoToDetails,
  type DocPendingNote,
} from "./WorkflowDocActionsContext";
import { docBranchSelection } from "./docBranchSelection";
import { DocOutline } from "./DocOutline";
import { DocStepSearch } from "./DocStepSearch";
import { DocAnnouncer } from "./DocAnnouncer";
import { DocAddStep, DocScope, type DocNoteProblem } from "./DocScope";
import { useRecordingLauncherStore } from "@/store/useRecordingLauncherStore";
import { DocSidePanel } from "./DocSidePanel";
import { DocText } from "./DocText";
import { DocEditBar } from "./DocEditing";
import {
  createDocEditApi,
  docDestinations,
  TOP_SCOPE,
  type DocGraphEdits,
  type DocPlace,
  type NodeType,
} from "./docEditApi";
import { countReferences, findOutputUsers, textValues } from "./docGraphEdits";
import { docStepTitle } from "./docStepRegistry";
import {
  docProblemEntries,
  docStepFixes,
  type DocStepFix,
} from "./docProblems";
import { customSettingsShortText } from "./docSettingsChips";
import { DocProblemsList } from "./DocProblemsList";
import { docProblemStep } from "./docProblemStep";
import { publishDocCriticalProblems } from "./docRunCheckStore";
import { docPromptUrlOffer } from "./docPromptUrl";
import { stepsLeftBy, useDocUnfinishedStore } from "./docUnfinished";
import { DocGettingStarted, DocStarterNote, DocTip } from "./DocStarter";
import { docTipIndex, type DocStarterProgress } from "./docStarterTips";
import { useDocLatestRunCompleted } from "./useDocLatestRunCompleted";
import {
  DocProposalCard,
  DocProposalProvider,
  DocUnanchoredRemovals,
  useDocProposal,
} from "./DocProposalView";

// The conditionals and branches that contain a step, outermost first.
type RevealPath = {
  branches: Array<[string, string]>;
  loops: string[];
  conditionals: string[];
};

function revealPath(items: DocItem[], id: string): RevealPath | null {
  for (const item of items) {
    if (item.kind === "note") continue;
    if (item.kind === "unplaced") {
      if (item.nodeIds.includes(id))
        return { branches: [], loops: [], conditionals: [] };
      continue;
    }
    if (item.nodeId === id)
      return { branches: [], loops: [], conditionals: [] };
    if (item.kind === "loop") {
      const found = revealPath(item.children, id);
      if (found)
        return {
          ...found,
          loops: [item.nodeId, ...found.loops],
        };
    }
    if (item.kind === "conditional")
      for (const branch of item.branches) {
        const found = revealPath(branch.children, id);
        if (found)
          return {
            ...found,
            branches: [[item.nodeId, branch.branchId], ...found.branches],
            conditionals: [item.nodeId, ...found.conditionals],
          };
      }
  }
  return null;
}

function docContainerIds(items: DocItem[]): string[] {
  return items.flatMap((item) => {
    if (item.kind === "loop")
      return [item.nodeId, ...docContainerIds(item.children)];
    if (item.kind === "conditional")
      return [
        item.nodeId,
        ...item.branches.flatMap((branch) => docContainerIds(branch.children)),
      ];
    return [];
  });
}

const OUTLINE_MIN_WIDTH = 640;

const REFERENCE = /\{\{\s*([A-Za-z_][A-Za-z0-9_]*)_output\b/g;
const PLAIN_OUTPUT = /^\s*([A-Za-z_][A-Za-z0-9_]*_output)(?![A-Za-z0-9_])/;

type WorkflowValidationProblem = {
  block_label: string | null;
  field: string | null;
  code: string;
  message: string;
  severity?: "error" | "warning";
};

const VALIDATE_DEBOUNCE_MS = 600;

const InputChip = forwardRef<
  HTMLElement,
  Omit<HTMLAttributes<HTMLElement>, "onClick"> & { onClick?: () => void }
>(function InputChip({ onClick, ...props }, ref) {
  return onClick ? (
    <button
      type="button"
      ref={ref as Ref<HTMLButtonElement>}
      onClick={onClick}
      {...props}
    />
  ) : (
    <span ref={ref as Ref<HTMLSpanElement>} {...props} />
  );
});

const REPLACE_WAIT_MS = 120_000;

export function WorkflowDocEditor({
  nodes: liveNodes,
  edges: liveEdges,
  edits,
}: {
  nodes: AppNode[];
  edges: Edge[];
  edits?: DocGraphEdits;
}) {
  const liveDescription = useWorkflowTitleStore((s) => s.description);
  const liveParameters = useWorkflowParametersStore((s) => s.parameters);
  const workflowId = useWorkflowScopeId();
  const credentialGetter = useCredentialGetter();
  const saveGeneration = useWorkflowHasChangesStore(
    (s) => (workflowId ? s.saveGenerationsByWorkflow[workflowId] : 0) ?? 0,
  );
  const scopeReadOnly = useWorkflowScopeReadOnly();
  const locked = useWorkflowYamlEditorStore(selectEditorMutationLocked);
  // While Copilot's proposal is pending, the Doc shows it read-only, as the comparison does.
  const proposal = useDocProposal(workflowId, liveNodes, liveEdges);
  const description = proposal ? proposal.description : liveDescription;
  const parameters = proposal?.parameters ?? liveParameters;
  const nodes = proposal?.nodes ?? liveNodes;
  const edges = proposal?.edges ?? liveEdges;
  // A demonstration anchors its insertion by step labels, so the Doc holds still while one runs.
  const demonstrating = useRecordingLauncherStore(
    (s) => s.demonstration !== null,
  );
  const editable = !scopeReadOnly && !locked && !proposal && !demonstrating;
  const showingProposal = proposal !== null;
  const setDocMounted = useWorkflowPresentationStore((s) => s.setDocMounted);
  useEffect(() => {
    setDocMounted(true);
    return () => setDocMounted(false);
  }, [setDocMounted]);
  // Workspace's undo shortcut listens on the document, so this holds wherever focus is: undo
  // would change the live graph hidden behind the read-only proposal.
  useEffect(() => {
    if (!showingProposal) return;
    const holdUndo = (event: KeyboardEvent) => {
      const key = event.key.toLowerCase();
      if (
        !(key === "z" && (event.ctrlKey || event.metaKey)) &&
        !(key === "y" && event.ctrlKey)
      )
        return;
      if (event.isComposing || isEditableTarget(event.target)) return;
      // The live browser's canvas takes its own keys; they never reach Workspace's undo.
      if (event.target instanceof HTMLCanvasElement) return;
      event.preventDefault();
      event.stopPropagation();
    };
    document.addEventListener("keydown", holdUndo, true);
    return () => document.removeEventListener("keydown", holdUndo, true);
  }, [showingProposal]);
  const postHog = usePostHog();
  const opened = useRef(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const panelOpener = useRef<HTMLElement | null>(null);
  // The step whose settings opened the panel, so Esc can put the caret back in its text.
  const panelStepId = useRef<string | null>(null);
  const learnButtonRef = useRef<HTMLButtonElement>(null);
  const [panelOpen, setPanelOpen] = useState(false);
  const [panelTab, setPanelTab] = useState("learn");
  const [selectedStepId, setSelectedStepId] = useState<string | null>(null);
  const [learn, setLearn] = useState<DocLearnContent>(docLearnContent[0]!);
  const [branches, setBranches] = useState<Record<string, string>>({});
  const [closedLoops, setClosedLoops] = useState<Set<string>>(() => new Set());
  const [closedConditionals, setClosedConditionals] = useState<Set<string>>(
    () => new Set(),
  );
  // Kept here, by step id, so a step's Details survives the remount a move causes and navigation can open it.
  const [detailsOpen, setDetailsOpen] = useState<ReadonlyMap<string, boolean>>(
    () => new Map(),
  );
  const setStepDetailsOpen = (id: string, open: boolean | undefined) =>
    setDetailsOpen((current) => {
      if (current.get(id) === open) return current;
      const next = new Map(current);
      if (open === undefined) next.delete(id);
      else next.set(id, open);
      return next;
    });
  const [searchOpen, setSearchOpen] = useState(false);
  const touch = useDocTouch();
  const locateRequest = useLocateBlockStore((s) => s.request);
  const clearLocate = useLocateBlockStore((s) => s.clearLocate);
  const [wide, setWide] = useState(false);
  const toastShowing = useToast().toasts.some((t) => t.open !== false);
  const [focusId, setFocusId] = useState<string | null>(null);
  const focusIdRef = useRef(focusId);
  focusIdRef.current = focusId;
  const [pendingNote, setPendingNote] = useState<DocPendingNote | null>(null);
  const [dragId, setDragId] = useState<string | null>(null);
  const [editingInput, setEditingInput] = useState<{
    key: string;
    isNew?: boolean;
  } | null>(null);
  // Closing the input editor that "@ > Create input" opened puts the caret back in the line the "@" came from.
  const inputEditorWasOpen = useRef(false);
  useEffect(() => {
    const open = editingInput !== null;
    const closed = inputEditorWasOpen.current && !open;
    inputEditorWasOpen.current = open;
    if (!closed) return;
    const frame = requestAnimationFrame(() => {
      const line = takeReferenceLine();
      const active = document.activeElement;
      if (!line || (active && active !== document.body)) return;
      const target = Array.from(
        rootRef.current?.querySelectorAll<HTMLElement>("[data-doc-line]") ?? [],
      ).filter((el) => el.getAttribute("aria-label") === line.label)[
        line.index
      ];
      if (!target) return;
      armNextCaretOffset(line.offset);
      target.focus();
    });
    return () => cancelAnimationFrame(frame);
  }, [editingInput]);
  // Inputs deleted here: their mentions stay in the text and show as missing.
  const [removedInputs, setRemovedInputs] = useState<Set<string>>(
    () => new Set(),
  );
  // Native drag events can end outside the doc, or not reach a drop target; any end clears the drag.
  useEffect(() => {
    if (!dragId) return;
    const clear = () => setDragId(null);
    window.addEventListener("dragend", clear);
    window.addEventListener("drop", clear);
    return () => {
      window.removeEventListener("dragend", clear);
      window.removeEventListener("drop", clear);
    };
  }, [dragId]);
  const [validationProblems, setValidationProblems] = useState<
    WorkflowValidationProblem[]
  >([]);
  const { data: browserTypeOptions } = useBrowserTypeOptionsQuery();
  const changedSettingsCount = useMemo(() => {
    const start = nodes.find((n) => n.type === "start" && !n.parentId);
    return start?.type === "start" && start.data.withWorkflowSettings
      ? changedSettingCount(start.data, { browserTypeOptions })
      : 0;
  }, [nodes, browserTypeOptions]);
  // The settings store is filled by the canvas Start node only once it renders.
  const finallyBlockLabel = useMemo(() => {
    const start = nodes.find((n) => n.type === "start" && !n.parentId);
    return start?.type === "start" && start.data.withWorkflowSettings
      ? start.data.finallyBlockLabel
      : null;
  }, [nodes]);
  const tree = useMemo(
    () => buildDocTree(nodes, edges, finallyBlockLabel),
    [nodes, edges, finallyBlockLabel],
  );
  const stepNumbers = useMemo(() => docStepNumbers(tree.items), [tree.items]);
  const previousNoteReader = useMemo(() => {
    const { previousByLabel } = graphNoteReadings(
      nodes,
      edges,
      finallyBlockLabel,
    );
    const idsByLabel = new Map(nodes.map((node) => [node.data.label, node.id]));
    const labelsById = new Map(nodes.map((node) => [node.id, node.data.label]));
    return (nodeId: string) => {
      const previous = previousByLabel.get(labelsById.get(nodeId) ?? "");
      return previous ? idsByLabel.get(previous) : undefined;
    };
  }, [nodes, edges, finallyBlockLabel]);
  const byId = useMemo(
    () => new Map(nodes.map((node) => [node.id, node])),
    [nodes],
  );
  const inputs = useMemo(
    () => getDocInputChips(parameters, nodes),
    [parameters, nodes],
  );
  const credentialKeys = useMemo(
    () =>
      new Set(
        parameters
          .filter(
            (p) =>
              p.parameterType !== "workflow" ||
              p.dataType === WorkflowParameterValueType.CredentialId,
          )
          .map((p) => p.key),
      ),
    [parameters],
  );
  const stepCount = countDocSteps(tree.items);
  const containerIds = useMemo(() => docContainerIds(tree.items), [tree.items]);
  const startId =
    nodes.find((n) => n.type === "start" && !n.parentId)?.id ?? null;
  const finallyId = useMemo(
    () =>
      nodes.find(
        (n) =>
          isWorkflowBlockNode(n) &&
          !n.parentId &&
          n.data.label === finallyBlockLabel,
      )?.id ?? null,
    [nodes, finallyBlockLabel],
  );
  const branchSelections = useMemo(() => {
    const selected = new Map<string, string>();
    const visit = (items: DocItem[]) => {
      for (const item of items) {
        if (item.kind === "loop") visit(item.children);
        if (item.kind !== "conditional") continue;
        selected.set(
          item.nodeId,
          docBranchSelection(item, branches[item.nodeId]).branchId,
        );
        item.branches.forEach((branch) => visit(branch.children));
      }
    };
    visit(tree.items);
    return selected;
  }, [tree, branches]);
  const selectBranch = (conditionalId: string, branchId: string) =>
    setBranches((current) => ({ ...current, [conditionalId]: branchId }));
  const startDemonstration = useRecordingLauncherStore(
    (s) => s.startDemonstration,
  );
  // An empty step a recording is meant to replace goes only once the recording is open, so a refused or failed start leaves it.
  const replaceOnRecording = useRef<{
    id: string;
    insertion: WorkflowCopilotDemonstrationInsertion;
    failures: number;
    timer: ReturnType<typeof setTimeout>;
  } | null>(null);
  const latestEdit = useRef<{
    removeBlankStep: (id: string) => boolean;
  } | null>(null);
  useEffect(
    () =>
      useRecordingLauncherStore.subscribe((state) => {
        const pending = replaceOnRecording.current;
        if (!pending) return;
        if (state.demonstrationStartFailures !== pending.failures) {
          clearTimeout(pending.timer);
          replaceOnRecording.current = null;
          return;
        }
        const status = state.demonstration;
        if (!status) return;
        if (status.phase === "stopped") {
          clearTimeout(pending.timer);
          replaceOnRecording.current = null;
        } else if (
          status.phase === "recording" &&
          status.insertion?.after_label === pending.insertion.after_label &&
          status.insertion?.before_label === pending.insertion.before_label
        ) {
          clearTimeout(pending.timer);
          replaceOnRecording.current = null;
          latestEdit.current?.removeBlankStep(pending.id);
        }
      }),
    [],
  );
  useEffect(
    () => () => {
      const waiting = replaceOnRecording.current;
      if (waiting) clearTimeout(waiting.timer);
      replaceOnRecording.current = null;
    },
    [],
  );
  const openStepSettings = (id: string) => {
    panelOpener.current = document.activeElement as HTMLElement;
    panelStepId.current = id;
    setSelectedStepId(id);
    setPanelTab("settings");
    setPanelOpen(true);
  };
  const baseEdit =
    editable && edits
      ? createDocEditApi({
          nodes,
          edges,
          edits,
          finallyId,
          workflowId: workflowId ?? "__global__",
          selectBranch,
          selectedBranch: (conditionalId) =>
            branchSelections.get(conditionalId),
          inputKeys: inputs.map((i) => i.key),
          credentialKeys,
        })
      : null;
  const edit = baseEdit && {
    ...baseEdit,
    insertStep: (place: DocPlace, nodeType: NodeType) => {
      const added = baseEdit.insertStep(place, nodeType);
      if (added) {
        // A login has nothing to run without its credential, so its settings open on it.
        if (nodeType === "login") openStepSettings(added);
      }
      return added;
    },
  };
  latestEdit.current = edit;
  // References to step outputs that no step saves any more, for the inline "missing" pills.
  const missingOutputs = useMemo(() => {
    const labels = new Set(
      nodes.filter(isWorkflowBlockNode).map((n) => n.data.label),
    );
    const parameterKeys = new Set(parameters.map((parameter) => parameter.key));
    const missing = new Set<string>();
    const addMissingOutput = (key: string) => {
      const label = key.replace(/_output$/, "");
      if (
        !key.endsWith("_output") ||
        labels.has(label) ||
        parameterKeys.has(key)
      )
        return;
      missing.add(key);
    };
    for (const node of nodes)
      if (isWorkflowBlockNode(node)) {
        for (const text of textValues(node.data))
          for (const match of text.matchAll(REFERENCE))
            addMissingOutput(`${match[1]}_output`);
        const data = node.data as Record<string, unknown>;
        if (Array.isArray(data.parameterKeys))
          for (const key of data.parameterKeys)
            if (typeof key === "string" && PLAIN_OUTPUT.test(key))
              addMissingOutput(key);
        for (const value of [data.loopVariableReference, data.loopValue])
          if (typeof value === "string") {
            const key = value.match(PLAIN_OUTPUT)?.[1];
            if (key) addMissingOutput(key);
          }
      }
    return missing;
  }, [nodes, parameters]);
  // What steps still need before they can run, found as the draft is typed; the server's check follows.
  // A step added a moment ago is unfinished, not wrong: its problems wait until the user leaves it, runs or saves.
  const unfinishedIds = useDocUnfinishedStore((s) => s.ids);
  const stepFixes = useMemo(() => {
    if (proposal) return new Map<string, DocStepFix[]>();
    const found = docStepFixes(nodes, editable);
    for (const id of unfinishedIds) found.delete(id);
    return found;
  }, [nodes, proposal, unfinishedIds, editable]);
  const unfinishedLabels = useMemo(
    () =>
      new Set(
        nodes.filter((n) => unfinishedIds.has(n.id)).map((n) => n.data.label),
      ),
    [nodes, unfinishedIds],
  );
  const shownProblems = useMemo(
    () =>
      validationProblems.filter(
        (problem) =>
          !problem.block_label || !unfinishedLabels.has(problem.block_label),
      ),
    [validationProblems, unfinishedLabels],
  );
  const problemEntries = useMemo(
    () =>
      docProblemEntries({
        nodes,
        items: tree.items,
        fixes: stepFixes,
        problems: shownProblems,
        editable,
      }),
    [nodes, tree, stepFixes, shownProblems, editable],
  );
  const problems = problemEntries.length;
  useEffect(() => {
    const byNodeId = new Map(nodes.map((node) => [node.id, node]));
    publishDocCriticalProblems(
      problemEntries
        .filter((entry) => entry.blocking)
        .map((entry) => {
          const node = entry.nodeId ? byNodeId.get(entry.nodeId) : undefined;
          const step = node ? docProblemStep(node) : null;
          return {
            key: entry.key,
            step: step
              ? step.type
                ? `${step.type} · ${step.name}`
                : step.name
              : null,
            message: entry.message,
          };
        }),
    );
  }, [nodes, problemEntries]);
  useEffect(() => () => publishDocCriticalProblems([]), []);
  const workflowProblems = useMemo(
    () => validationProblems.filter((problem) => !problem.block_label),
    [validationProblems],
  );
  const noteProblems = useMemo(() => {
    const idsByLabel = new Map(nodes.map((node) => [node.data.label, node.id]));
    const result = new Map<string, DocNoteProblem>();
    for (const problem of shownProblems) {
      if (
        problem.code === "credential_in_note" &&
        problem.block_label &&
        (problem.field === "notes" || problem.field === "trailing_notes")
      ) {
        const nodeId = idsByLabel.get(problem.block_label);
        if (!nodeId) continue;
        result.set(
          `${nodeId}:${problem.field === "notes" ? "above" : "trailing"}`,
          { code: problem.code, message: problem.message },
        );
        continue;
      }
      if (!problem.block_label) continue;
      const stepId = idsByLabel.get(problem.block_label);
      if (stepId) {
        const previous = result.get(`${stepId}:step`);
        const messages = previous ? previous.message.split("\n") : [];
        if (messages.includes(problem.message)) continue;
        result.set(`${stepId}:step`, {
          code: problem.code,
          message: [...messages, problem.message].join("\n"),
        });
      }
    }
    for (const [nodeId, fixes] of stepFixes)
      result.set(`${nodeId}:fix`, {
        code: "needs_setting",
        message: fixes.map((fix) => fix.message).join("\n"),
      });
    return result;
  }, [nodes, shownProblems, stepFixes]);

  // Step warnings sit in the same map but are not notes and do not need fixing.
  // Counts notes, not problems: one credential named in two notes is one problem and two notes to fix.
  const noteProblemCount = [...noteProblems.keys()].filter(
    (key) => !key.endsWith(":step") && !key.endsWith(":fix"),
  ).length;
  // The panel is exempt from leave detection while it shows the step's settings; closing it or switching away is leaving.
  useEffect(() => {
    const { ids, settle } = useDocUnfinishedStore.getState();
    settle(
      [...ids].filter(
        (id) =>
          !(panelOpen && panelTab === "settings" && id === selectedStepId),
      ),
    );
  }, [panelOpen, panelTab, selectedStepId]);
  // A new step turns into a normal one when the user moves on: clicks or tabs elsewhere, or presses a Run/Save shortcut.
  useEffect(() => {
    const store = useDocUnfinishedStore.getState;
    const leave = (event: Event) => {
      const { ids, settle } = store();
      if (ids.size) settle(stepsLeftBy(ids, event.target));
    };
    const shortcut = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && /^(s|Enter)$/i.test(event.key))
        store().clear();
    };
    document.addEventListener("pointerdown", leave, true);
    document.addEventListener("focusin", leave, true);
    document.addEventListener("keydown", shortcut, true);
    return () => {
      document.removeEventListener("pointerdown", leave, true);
      document.removeEventListener("focusin", leave, true);
      document.removeEventListener("keydown", shortcut, true);
      store().clear();
    };
  }, []);
  const latestRunCompleted = useDocLatestRunCompleted(workflowId ?? null);
  const starter = useDocStarter(
    workflowId ?? null,
    Boolean(edit) && stepCount === 0 && !description?.trim(),
  );
  const starterProgress: DocStarterProgress = {
    steps: stepCount,
    usedReference: nodes.some(
      (node) =>
        isWorkflowBlockNode(node) &&
        textValues(node.data).some((value) => value.includes("{{")),
    ),
    nested: nodes.some((node) => isWorkflowBlockNode(node) && node.parentId),
    hasContainer: nodes.some(
      (node) => node.type === "loop" || node.type === "conditional",
    ),
    problems,
    ran: latestRunCompleted,
  };
  // One onboarding aid at a time: the checklist waits while a tip is showing.
  const showTip =
    starter.active && docTipIndex(starterProgress, starter.seenTips) >= 0;
  const reached = docStarterDone(starterProgress);
  const { markDone } = starter;
  useEffect(() => {
    if (starter.active) markDone(reached);
    // `reached` is a fresh array each render; its contents are the dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [starter.active, reached.join(",")]);

  // Checks the draft as it stands, so a credential typed into a note is flagged before the save.
  useEffect(() => {
    if (proposal) {
      setValidationProblems([]);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      const saveData = useWorkflowHasChangesStore.getState().getSaveData();
      if (!saveData) {
        setValidationProblems([]);
        return;
      }
      void getClient(credentialGetter)
        .then((client) =>
          client.post(
            "/workflows/validate",
            buildWorkflowSaveRequest(saveData, { explicitDocFields: true }),
          ),
        )
        .then((response) => {
          if (!cancelled)
            setValidationProblems(
              (response.data?.problems ?? []) as WorkflowValidationProblem[],
            );
        })
        .catch((error) => {
          const detail = isAxiosError<{ detail?: unknown }>(error)
            ? error.response?.data?.detail
            : undefined;
          const reason =
            typeof detail === "string"
              ? detail
              : error instanceof Error
                ? error.message
                : "Request failed.";
          if (!cancelled)
            setValidationProblems((current) => [
              ...current.filter(
                (problem) => problem.code !== "validation_failed",
              ),
              {
                block_label: null,
                field: null,
                code: "validation_failed",
                message: `Couldn't check this workflow: ${reason}`,
              },
            ]);
        });
    }, VALIDATE_DEBOUNCE_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [
    credentialGetter,
    nodes,
    edges,
    liveParameters,
    saveGeneration,
    proposal,
  ]);

  useEffect(() => {
    if (!opened.current) {
      opened.current = true;
      postHog?.capture("studio.doc.opened", {
        workflow_permanent_id: workflowId,
      });
    }
  }, [postHog, workflowId]);
  useEffect(() => {
    rootRef.current?.focus();
    const root = rootRef.current;
    const observer = new ResizeObserver(() => {
      const width = root?.clientWidth ?? 0;
      setWide(width >= OUTLINE_MIN_WIDTH);
    });
    if (root) observer.observe(root);
    return () => {
      observer.disconnect();
      if (useWorkflowYamlEditorStore.getState().active) return;
      document
        .querySelector<HTMLButtonElement>(
          '[aria-label="Editor view"] button[aria-pressed="true"]',
        )
        ?.focus();
    };
  }, []);

  const closePanel = () => {
    setPanelOpen(false);
    const stepLine = docStepLine(rootRef.current, panelStepId.current);
    panelStepId.current = null;
    (panelOpener.current?.isConnected
      ? panelOpener.current
      : (stepLine ?? learnButtonRef.current)
    )?.focus();
  };
  const goTo = (id: string, details?: DocGoToDetails) => {
    const path = revealPath(tree.items, id);
    if (path?.branches.length)
      setBranches((current) => ({
        ...current,
        ...Object.fromEntries(path.branches),
      }));
    if (path?.loops.length)
      setClosedLoops((current) => {
        if (!path.loops.some((loopId) => current.has(loopId))) return current;
        const next = new Set(current);
        for (const loopId of path.loops) next.delete(loopId);
        return next;
      });
    if (path?.conditionals.length)
      setClosedConditionals((current) => {
        if (!path.conditionals.some((id) => current.has(id))) return current;
        const next = new Set(current);
        for (const conditionalId of path.conditionals)
          next.delete(conditionalId);
        return next;
      });
    // A step whose line is being typed in only moved (Tab).
    if (details && focusIdRef.current !== id)
      setStepDetailsOpen(id, details === "open" ? true : undefined);
    requestAnimationFrame(() => {
      const target = scrollRef.current?.querySelector<HTMLElement>(
        `[data-doc-node-id="${CSS.escape(id)}"]`,
      );
      target?.scrollIntoView({ block: "center", behavior: "smooth" });
      if (!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches)
        target?.animate?.(
          [
            { backgroundColor: "hsl(var(--muted))" },
            { backgroundColor: "transparent" },
          ],
          { duration: 1200, easing: "ease-out" },
        );
      // A step whose line is being typed in (Tab just moved it) gets the caret back on that line.
      const line =
        focusIdRef.current === id
          ? target?.querySelector<HTMLElement>('[role="textbox"]')
          : null;
      (line ?? target)?.focus({ preventScroll: true });
    });
  };
  // The run-blocking panel's Locate lands here while the canvas is covered.
  const goToRef = useRef(goTo);
  goToRef.current = goTo;
  useEffect(() => {
    if (!locateRequest) return;
    if (
      locateRequest.expectedParentId &&
      !revealPath(tree.items, locateRequest.nodeId)?.loops.includes(
        locateRequest.expectedParentId,
      )
    )
      return;
    goToRef.current(locateRequest.nodeId);
    clearLocate();
  }, [locateRequest, clearLocate, tree]);
  const openInputs = () =>
    useWorkflowPanelStore
      .getState()
      .setWorkflowPanelState({ active: true, content: "parameters" });
  // The same store and step rewrites as the Inputs panel, applied as one Doc edit.
  const applyInputs = (
    nextParameters: typeof liveParameters,
    nextNodes: ((nodes: AppNode[]) => AppNode[]) | null,
  ) => {
    useWorkflowParametersStore.getState().setParametersFromUser(nextParameters);
    useWorkflowHasChangesStore.getState().setHasChanges(true);
    if (nextNodes) edits?.update(nextNodes);
  };
  const forgetRemovedInput = (key: string) =>
    setRemovedInputs((current) => {
      if (!current.has(key)) return current;
      const next = new Set(current);
      next.delete(key);
      return next;
    });
  const changesBeforeNewInput = useRef(false);
  const addInput = () => {
    changesBeforeNewInput.current =
      useWorkflowHasChangesStore.getState().hasChanges;
    const key = newDocInputKey(liveParameters, liveNodes);
    applyInputs(
      [
        ...liveParameters,
        {
          key,
          parameterType: "workflow",
          dataType: WorkflowParameterValueType.String,
          description: null,
          defaultValue: null,
        },
      ],
      null,
    );
    forgetRemovedInput(key);
    setEditingInput({ key, isNew: true });
  };
  const saveInput = (oldKey: string, parameter: ParametersState[number]) => {
    const nameProblem = docInputNameProblem(
      parameter.key,
      oldKey,
      liveNodes,
      liveParameters
        .filter((existing) => existing.key !== oldKey)
        .map((existing) => existing.key),
    );
    if (nameProblem) {
      toast({
        title: "Failed to save input",
        description: nameProblem,
        variant: "destructive",
      });
      return;
    }
    const rewritten =
      oldKey === parameter.key ? 0 : countReferences(liveNodes, oldKey);
    const result = saveDocInput(liveParameters, oldKey, parameter);
    applyInputs(result.parameters, result.nodes);
    forgetRemovedInput(parameter.key);
    if (rewritten > 0)
      toast({
        title: `Renamed ${oldKey} to ${parameter.key}`,
        description: `${rewritten} reference${rewritten === 1 ? "" : "s"} updated`,
      });
    setEditingInput(null);
  };
  // Closing the form of a new input nobody filled in or used drops it, so "+ Input" then Escape leaves nothing behind.
  const closeInputEditor = () => {
    const editing = editingInput;
    setEditingInput(null);
    if (!editing?.isNew) return;
    const parameter = liveParameters.find((p) => p.key === editing.key);
    const untouched =
      parameter?.parameterType === "workflow" &&
      !parameter.description &&
      parameter.defaultValue == null &&
      parameter.dataType === WorkflowParameterValueType.String;
    if (untouched && countReferences(liveNodes, editing.key) === 0) {
      applyInputs(
        liveParameters.filter((p) => p.key !== editing.key),
        null,
      );
      // The agent is back to what it was before "+ Input", so it isn't unsaved.
      if (!changesBeforeNewInput.current)
        useWorkflowHasChangesStore.getState().setHasChanges(false);
    }
  };
  const deleteInput = (key: string) => {
    const used = inputs.some((input) => input.key === key && !input.unused);
    const result = removeDocInput(liveParameters, key);
    if ("refused" in result) {
      toast({ title: result.refused, variant: "destructive" });
      return;
    }
    applyInputs(result.parameters, result.nodes);
    setRemovedInputs((current) => new Set(current).add(key));
    setEditingInput(null);
    toast({
      title: used
        ? "Input deleted. Its mentions now show as missing."
        : "Input deleted",
    });
  };

  return (
    <WorkflowDocActionsContext.Provider
      value={{
        editable,
        runControlsVisible: !scopeReadOnly && !proposal,
        openSettings: openStepSettings,
        openVisual: () =>
          useWorkflowPresentationStore.getState().setPresentation("visual"),
        goTo,
        selectedBranch: (conditionalId) => branchSelections.get(conditionalId),
        previousNoteReader,
        promptUrlOffer: (node) => docPromptUrlOffer({ nodes, edges }, node),
        stepNumbers,
        selectBranch,
        loopOpen: (loopId) => !closedLoops.has(loopId),
        toggleLoop: (loopId) =>
          setClosedLoops((current) => {
            const next = new Set(current);
            if (!next.delete(loopId)) next.add(loopId);
            return next;
          }),
        detailsOpen: (id) => detailsOpen.get(id),
        setDetailsOpen: setStepDetailsOpen,
        conditionalOpen: (conditionalId) =>
          !closedConditionals.has(conditionalId),
        toggleConditional: (conditionalId) =>
          setClosedConditionals((current) => {
            const next = new Set(current);
            if (!next.delete(conditionalId)) next.add(conditionalId);
            return next;
          }),
        edit,
        destinations: () =>
          docDestinations(tree.items, byId, (node) => docStepTitle(node)),
        demonstrateAt: (place, replacing) => {
          const insertion = startDemonstration
            ? (edit?.demonstrationInsertion(place, replacing) ?? null)
            : null;
          return insertion && startDemonstration
            ? () => {
                // The wait ends when the recording opens or the start is refused or fails; the timer is only a safety net.
                const waiting = replaceOnRecording.current;
                if (waiting) clearTimeout(waiting.timer);
                replaceOnRecording.current = replacing
                  ? {
                      id: replacing,
                      insertion,
                      failures:
                        useRecordingLauncherStore.getState()
                          .demonstrationStartFailures,
                      timer: setTimeout(() => {
                        replaceOnRecording.current = null;
                      }, REPLACE_WAIT_MS),
                    }
                  : null;
                startDemonstration({ insertion, keepEditor: true });
              }
            : null;
        },
        outputUsers: (id) =>
          findOutputUsers(nodes, id).map((node) => ({
            id: node.id,
            label: node.data.label,
            text: docStepTitle(node),
          })),
        createInput: edit
          ? (key) => {
              const parsed = parseDocInputForm(
                {
                  key,
                  dataType: WorkflowParameterValueType.String,
                  defaultText: "",
                  description: "",
                },
                null,
                liveParameters,
                liveNodes,
              );
              if ("error" in parsed) {
                toast({ title: parsed.error, variant: "destructive" });
                return false;
              }
              changesBeforeNewInput.current =
                useWorkflowHasChangesStore.getState().hasChanges;
              applyInputs([...liveParameters, parsed.parameter], null);
              forgetRemovedInput(key);
              setEditingInput({ key, isNew: true });
              return true;
            }
          : null,
        missingReference: (reference) => {
          const root = reference.split(/[.\s|[]/)[0] ?? "";
          return (
            missingOutputs.has(root) ||
            (removedInputs.has(root) &&
              !parameters.some((parameter) => parameter.key === root))
          );
        },
        focusId,
        setFocusId,
        pendingNote,
        setPendingNote,
        dragId,
        setDragId,
      }}
    >
      <div
        ref={rootRef}
        tabIndex={-1}
        role="region"
        aria-label="Workflow document"
        // Covers the whole pane and pads below the run-blocking card: a gap above the Doc would show the canvas underneath.
        className="absolute inset-0 z-30 flex min-w-0 bg-background pt-[var(--run-blocking-surface-bottom,0px)] text-foreground outline-none"
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.stopPropagation();
            if (panelOpen) closePanel();
          }
          if (
            event.key.toLowerCase() === "k" &&
            (event.metaKey || event.ctrlKey)
          ) {
            event.preventDefault();
            event.stopPropagation();
            setSearchOpen(true);
          }
          // "/" with no line focused adds a step at the end, as it would on a line.
          if (
            event.key === "/" &&
            event.target === rootRef.current &&
            !event.metaKey &&
            !event.ctrlKey
          ) {
            const adders =
              scrollRef.current?.querySelectorAll<HTMLButtonElement>(
                "[data-doc-add-step]",
              );
            const last = adders?.[adders.length - 1];
            if (last) {
              event.preventDefault();
              last.click();
            }
          }
        }}
      >
        <div ref={scrollRef} className="min-w-0 flex-1 overflow-y-auto">
          <div
            className={cn(
              // From sm up, the left margin holds each step's grip beside a loop's arrow.
              "relative mx-auto w-full max-w-[820px] pb-40 pl-8 pr-8 pt-7 sm:pl-16 sm:pt-11",
              wide && "sm:pr-10",
            )}
          >
            <div
              className={cn(
                "sm:absolute sm:inset-x-0 sm:top-2 sm:pl-16 sm:pr-8",
                wide && "sm:pr-10",
              )}
            >
              <DocEditBar
                floating={Boolean(edit)}
                proposalPending={Boolean(proposal)}
              />
            </div>
            <div className="mb-[26px] flex flex-wrap items-center gap-1.5 border-b border-border pb-[18px]">
              <span className="mr-1 text-[12.5px] text-[hsl(0_0%_60%)] dark:text-[hsl(215_14%_52%)]">
                Inputs
              </span>
              {inputs.map((input) => {
                const parameter = parameters.find((p) => p.key === input.key);
                const inDoc =
                  Boolean(edit) && parameter && isDocEditableInput(parameter);
                const chip = (
                  // A proposal's inputs are Copilot's, not the live workflow's, so they don't open the Inputs panel.
                  <InputChip
                    key={input.key}
                    onClick={
                      proposal
                        ? undefined
                        : inDoc
                          ? () => setEditingInput({ key: input.key })
                          : openInputs
                    }
                    aria-label={`Input ${input.key}${input.missing ? ", needed to run" : ""}${parameter?.parameterType === "context" ? `, uses data from ${parameter.sourceParameterKey}` : ""}${input.unused ? ", not used yet" : ""}`}
                    title={
                      input.missing
                        ? "Needed to run"
                        : parameter?.parameterType === "context"
                          ? `Uses data from ${parameter.sourceParameterKey}${input.unused ? "; not used by any step" : ""}`
                          : input.unused
                            ? NOT_USED_TITLE
                            : undefined
                    }
                    className={cn(
                      "inline-flex max-w-full items-center gap-[5px] whitespace-nowrap rounded-md border border-border bg-background py-0.5 pl-[7px] pr-[9px] text-[13px] outline-none focus-visible:ring-2 focus-visible:ring-ring",
                      !proposal && "hover:bg-muted",
                      input.missing &&
                        "border-[#B45B0A] bg-[#FBEEE2] dark:border-[#E79A55] dark:bg-[rgba(231,154,85,.15)]",
                      input.unused && "opacity-[.55]",
                    )}
                  >
                    <span className="truncate">{input.key}</span>
                    {(input.missing || input.value) && (
                      <em
                        title={input.missing ? undefined : input.valueTitle}
                        className={cn(
                          "max-w-[240px] truncate not-italic text-muted-foreground",
                          input.missing && "text-[#B45B0A] dark:text-[#E79A55]",
                        )}
                      >
                        {input.missing ? "needed to run" : input.value}
                      </em>
                    )}
                  </InputChip>
                );
                if (!inDoc || !parameter || !isDocEditableInput(parameter))
                  return chip;
                return (
                  <DocInputEditor
                    key={input.key}
                    open={editingInput?.key === input.key}
                    type={
                      parameter.parameterType === "context"
                        ? WorkflowEditorParameterTypes.Context
                        : WorkflowEditorParameterTypes.Workflow
                    }
                    initialValues={parameter}
                    isNew={Boolean(
                      editingInput?.isNew && editingInput.key === input.key,
                    )}
                    onSave={(value) => saveInput(input.key, value)}
                    onDelete={() => deleteInput(input.key)}
                    onClose={closeInputEditor}
                  >
                    {chip}
                  </DocInputEditor>
                );
              })}
              {edit && (
                <button
                  type="button"
                  onClick={addInput}
                  className="inline-flex items-center whitespace-nowrap rounded-md border border-dashed border-border bg-background py-0.5 pl-[7px] pr-[9px] text-[13px] text-muted-foreground outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
                >
                  + Input
                </button>
              )}
              {startId && !proposal && (
                <button
                  type="button"
                  aria-expanded={
                    panelOpen &&
                    panelTab === "settings" &&
                    selectedStepId === startId
                  }
                  onClick={() => {
                    panelOpener.current = document.activeElement as HTMLElement;
                    panelStepId.current = null;
                    setSelectedStepId(startId);
                    setPanelTab("settings");
                    setPanelOpen(true);
                  }}
                  className="ml-auto inline-flex items-center gap-[5px] whitespace-nowrap rounded-md border border-border bg-background py-0.5 pl-[7px] pr-[9px] text-[13px] text-muted-foreground outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <GearIcon className="size-[13px]" />
                  Agent settings
                  {changedSettingsCount > 0
                    ? ` · ${customSettingsShortText(changedSettingsCount)}`
                    : ""}
                </button>
              )}
              {inputs.length === 0 && !edit && (
                <span className="text-[12.5px] text-muted-foreground">
                  None
                </span>
              )}
            </div>
            <div className="-mt-2.5 mb-3.5 flex flex-wrap items-center gap-x-2.5 gap-y-1.5 text-[12.5px] text-muted-foreground">
              <span>
                {stepCount} step{stepCount === 1 ? "" : "s"}
              </span>
              {problems > 0 && (
                <DocProblemsList
                  entries={problemEntries}
                  byId={byId}
                  goTo={goTo}
                />
              )}
              <button
                type="button"
                onClick={() => setSearchOpen(true)}
                className="rounded-md px-2 py-[3px] outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
              >
                Search
                {!touch && (
                  <>
                    {" "}
                    <kbd className="text-[11px] text-muted-foreground">⌘K</kbd>
                  </>
                )}
              </button>
              {containerIds.length >= 2 && (
                <>
                  <button
                    type="button"
                    onClick={() => {
                      setClosedLoops(new Set(containerIds));
                      setClosedConditionals(new Set(containerIds));
                    }}
                    className="rounded-md px-2 py-[3px] outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    Collapse all
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setClosedLoops(new Set());
                      setClosedConditionals(new Set());
                    }}
                    className="rounded-md px-2 py-[3px] outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    Expand all
                  </button>
                </>
              )}
              <span className="flex-1" />
              <button
                ref={learnButtonRef}
                type="button"
                aria-expanded={panelOpen && panelTab === "learn"}
                onClick={() => {
                  panelOpener.current = learnButtonRef.current;
                  panelStepId.current = null;
                  setPanelTab("learn");
                  setPanelOpen(true);
                }}
                className="inline-flex items-center gap-1.5 rounded-md border border-border px-2 py-[3px] outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
              >
                <QuestionMarkCircledIcon className="size-3.5" />
                How this view works
              </button>
            </div>
            {noteProblemCount > 0 && (
              <div className="mb-4 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-800 dark:text-amber-200">
                {noteProblemCount} note{noteProblemCount === 1 ? "" : "s"}{" "}
                {noteProblemCount === 1 ? "needs" : "need"} fixing
              </div>
            )}
            {workflowProblems.length > 0 && (
              <div
                role="alert"
                className="mb-4 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-800 dark:text-amber-200"
              >
                {workflowProblems.map((problem, index) => (
                  <p key={`${problem.code}:${index}`}>{problem.message}</p>
                ))}
              </div>
            )}
            {description?.trim() && (
              <p className="mb-3 text-base leading-[1.55] text-muted-foreground">
                <DocText value={description} />
              </p>
            )}
            {proposal && <DocProposalCard proposal={proposal} goTo={goTo} />}
            {edit && stepCount === 0 && !description?.trim() && (
              <DocStarterNote />
            )}
            <DocProposalProvider value={proposal}>
              <DocScope
                items={tree.items}
                byId={byId}
                scope={TOP_SCOPE}
                noteProblems={noteProblems}
              />
              <DocUnanchoredRemovals />
              <DocAnnouncer />
              {edit && (
                <div className="mt-3">
                  <DocAddStep
                    place={{
                      scope: TOP_SCOPE,
                      index: Number.MAX_SAFE_INTEGER,
                    }}
                    label="Add step"
                  />
                  {showTip && (
                    <DocTip
                      progress={starterProgress}
                      seen={starter.seenTips}
                      onSeen={starter.seeTip}
                    />
                  )}
                </div>
              )}
            </DocProposalProvider>
          </div>
        </div>
        {edit && starter.active && !showTip && !(panelOpen && !wide) && (
          <DocGettingStarted
            progress={starterProgress}
            kept={starter.done}
            onHide={starter.hide}
            folded={toastShowing}
            compact={!wide}
            clearOfPanel={panelOpen}
          />
        )}
        {wide && !panelOpen && (
          <DocOutline items={tree.items} byId={byId} scrollRef={scrollRef} />
        )}
        <DocStepSearch
          open={searchOpen}
          onOpenChange={setSearchOpen}
          items={tree.items}
          byId={byId}
          goTo={goTo}
        />
        {panelOpen && (
          <DocSidePanel
            tab={panelTab}
            onTabChange={setPanelTab}
            selectedStep={selectedStepId ? byId.get(selectedStepId) : undefined}
            editable={editable}
            settingsReadable={!proposal}
            learn={learn}
            onLearn={setLearn}
            onClose={closePanel}
          />
        )}
      </div>
    </WorkflowDocActionsContext.Provider>
  );
}
