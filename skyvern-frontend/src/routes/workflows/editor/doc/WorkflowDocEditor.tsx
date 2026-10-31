import { graphNoteReadings } from "./noteSequences";
import {
  type HTMLAttributes,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { buildWorkflowSaveRequest } from "../workflowYamlDocument";
import type { Edge } from "@xyflow/react";
import { QuestionMarkCircledIcon } from "@radix-ui/react-icons";
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
import { WorkflowParameterValueType } from "../../types/workflowTypes";
import { isWorkflowBlockNode, type AppNode } from "../nodes";
import { useLocateBlockStore } from "../runValidation/useLocateBlockStore";
import { isEditableTarget } from "../editableTarget";
import {
  useWorkflowScopeId,
  useWorkflowScopeReadOnly,
} from "../WorkflowScopeContext";
import { buildDocTree, countDocSteps, type DocItem } from "./buildDocTree";
import { docLearnContent, type DocLearnContent } from "./docLearnContent";
import { getDocInputChips } from "./docInputChips";
import {
  WorkflowDocActionsContext,
  type DocPendingNote,
} from "./WorkflowDocActionsContext";
import { docBranchSelection } from "./docBranchSelection";
import { useDocRun } from "./useDocRun";
import { DocLiveIndicator } from "./DocLiveIndicator";
import { DocOutline } from "./DocOutline";
import { DocAddStep, DocScope, type DocNoteProblem } from "./DocScope";
import { DocSidePanel } from "./DocSidePanel";
import { DocText } from "./DocText";
import { DocEditBar } from "./DocEditing";
import {
  createDocEditApi,
  docDestinations,
  TOP_SCOPE,
  type DocGraphEdits,
} from "./docEditApi";
import { findOutputUsers } from "./docGraphEdits";
import { docStepTitle } from "./docStepRegistry";
import {
  DocProposalCard,
  DocProposalProvider,
  DocUnanchoredRemovals,
  useDocProposal,
} from "./DocProposalView";

// The conditionals and branches that contain a step, outermost first.
type RevealPath = { branches: Array<[string, string]>; loops: string[] };

function revealPath(items: DocItem[], id: string): RevealPath | null {
  for (const item of items) {
    if (item.kind === "note") continue;
    if (item.kind === "unplaced") {
      if (item.nodeIds.includes(id)) return { branches: [], loops: [] };
      continue;
    }
    if (item.nodeId === id) return { branches: [], loops: [] };
    if (item.kind === "loop") {
      const found = revealPath(item.children, id);
      if (found) return { ...found, loops: [item.nodeId, ...found.loops] };
    }
    if (item.kind === "conditional")
      for (const branch of item.branches) {
        const found = revealPath(branch.children, id);
        if (found)
          return {
            ...found,
            branches: [[item.nodeId, branch.branchId], ...found.branches],
          };
      }
  }
  return null;
}

const OUTLINE_MIN_WIDTH = 640;

const REFERENCE = /\{\{\s*([A-Za-z_][A-Za-z0-9_]*)_output\b/g;
const PLAIN_OUTPUT = /^\s*([A-Za-z_][A-Za-z0-9_]*_output)(?![A-Za-z0-9_])/;

function textValues(value: unknown, out: string[] = []): string[] {
  if (typeof value === "string") out.push(value);
  else if (Array.isArray(value)) value.forEach((v) => textValues(v, out));
  else if (value && typeof value === "object")
    Object.values(value).forEach((v) => textValues(v, out));
  return out;
}

type WorkflowValidationProblem = {
  block_label: string | null;
  field: string | null;
  code: string;
  message: string;
};

const VALIDATE_DEBOUNCE_MS = 600;

function InputChip({
  onClick,
  ...props
}: Omit<HTMLAttributes<HTMLElement>, "onClick"> & {
  onClick?: () => void;
}) {
  return onClick ? (
    <button type="button" onClick={onClick} {...props} />
  ) : (
    <span {...props} />
  );
}

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
  const editable = !scopeReadOnly && !locked && !proposal;
  const showingProposal = proposal !== null;
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
  const learnButtonRef = useRef<HTMLButtonElement>(null);
  const [panelOpen, setPanelOpen] = useState(false);
  const [panelTab, setPanelTab] = useState("learn");
  const [selectedStepId, setSelectedStepId] = useState<string | null>(null);
  const [learn, setLearn] = useState<DocLearnContent>(docLearnContent[0]!);
  const [branches, setBranches] = useState<Record<string, string>>({});
  const [closedLoops, setClosedLoops] = useState<Set<string>>(() => new Set());
  const locateRequest = useLocateBlockStore((s) => s.request);
  const clearLocate = useLocateBlockStore((s) => s.clearLocate);
  const [wide, setWide] = useState(false);
  const [focusId, setFocusId] = useState<string | null>(null);
  const [pendingNote, setPendingNote] = useState<DocPendingNote | null>(null);
  const [dragId, setDragId] = useState<string | null>(null);
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
  const { blocksByLabel } = useDocRun();
  const branchSelections = useMemo(() => {
    const selected = new Map<string, string>();
    const visit = (items: DocItem[]) => {
      for (const item of items) {
        if (item.kind === "loop") visit(item.children);
        if (item.kind !== "conditional") continue;
        const node = byId.get(item.nodeId);
        if (node && isWorkflowBlockNode(node))
          selected.set(
            item.nodeId,
            docBranchSelection(
              item,
              node.data.label,
              branches[item.nodeId],
              byId,
              blocksByLabel,
            ).selected.branchId,
          );
        item.branches.forEach((branch) => visit(branch.children));
      }
    };
    visit(tree.items);
    return selected;
  }, [tree, byId, blocksByLabel, branches]);
  const selectBranch = (conditionalId: string, branchId: string) =>
    setBranches((current) => ({ ...current, [conditionalId]: branchId }));
  const edit =
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
  const problems = validationProblems.length;
  const workflowProblems = useMemo(
    () => validationProblems.filter((problem) => !problem.block_label),
    [validationProblems],
  );
  const noteProblems = useMemo(() => {
    const idsByLabel = new Map(nodes.map((node) => [node.data.label, node.id]));
    const result = new Map<string, DocNoteProblem>();
    for (const problem of validationProblems) {
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
    return result;
  }, [nodes, validationProblems]);

  // Step warnings sit in the same map but are not notes and do not need fixing.
  const noteProblemCount = [...noteProblems.keys()].filter(
    (key) => !key.endsWith(":step"),
  ).length;

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
    const observer = new ResizeObserver(() =>
      setWide((root?.clientWidth ?? 0) >= OUTLINE_MIN_WIDTH),
    );
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
    (panelOpener.current?.isConnected
      ? panelOpener.current
      : learnButtonRef.current
    )?.focus();
  };
  const goTo = (id: string) => {
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
    requestAnimationFrame(() => {
      const target = scrollRef.current?.querySelector<HTMLElement>(
        `[data-doc-node-id="${CSS.escape(id)}"]`,
      );
      target?.scrollIntoView({ block: "center", behavior: "smooth" });
      target?.focus({ preventScroll: true });
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

  return (
    <WorkflowDocActionsContext.Provider
      value={{
        editable,
        runControlsVisible: !scopeReadOnly && !proposal,
        openSettings: (id) => {
          panelOpener.current = document.activeElement as HTMLElement;
          setSelectedStepId(id);
          setPanelTab("settings");
          setPanelOpen(true);
        },
        openVisual: () =>
          useWorkflowPresentationStore.getState().setPresentation("visual"),
        goTo,
        selectedBranch: (conditionalId) => branchSelections.get(conditionalId),
        previousNoteReader,
        selectBranch,
        loopOpen: (loopId) => !closedLoops.has(loopId),
        toggleLoop: (loopId) =>
          setClosedLoops((current) => {
            const next = new Set(current);
            if (!next.delete(loopId)) next.add(loopId);
            return next;
          }),
        edit,
        destinations: () =>
          docDestinations(tree.items, byId, (node) => docStepTitle(node)),
        outputUsers: (id) =>
          findOutputUsers(nodes, id).map((node) => ({
            id: node.id,
            label: node.data.label,
            text: docStepTitle(node),
          })),
        missingReference: (reference) =>
          missingOutputs.has(reference.split(/[.\s|[]/)[0] ?? ""),
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
        }}
      >
        <div ref={scrollRef} className="min-w-0 flex-1 overflow-y-auto">
          <div
            className={cn(
              "mx-auto w-full max-w-[820px] px-8 pb-40 pt-7 sm:pt-11",
              wide && "sm:px-10",
            )}
          >
            <DocEditBar
              problems={problems}
              reserve={Boolean(edit)}
              proposalPending={Boolean(proposal)}
            />
            <div className="mb-[26px] flex flex-wrap items-center gap-1.5 border-b border-border pb-[18px]">
              <span className="mr-1 text-[12.5px] text-muted-foreground">
                Inputs
              </span>
              {inputs.map((input) => (
                // A proposal's inputs are Copilot's, not the live workflow's, so they don't open the Inputs panel.
                <InputChip
                  key={input.key}
                  onClick={proposal ? undefined : openInputs}
                  title={
                    input.missing
                      ? "Needed to run"
                      : input.unused
                        ? "Not used by any step"
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
                      className={cn(
                        "max-w-[160px] truncate not-italic text-muted-foreground",
                        input.missing && "text-[#B45B0A] dark:text-[#E79A55]",
                      )}
                    >
                      {input.missing ? "needed to run" : input.value}
                    </em>
                  )}
                </InputChip>
              ))}
              {inputs.length === 0 && (
                <span className="text-[12.5px] text-muted-foreground">
                  None
                </span>
              )}
            </div>
            <div className="-mt-2.5 mb-3.5 flex flex-wrap items-center gap-x-2.5 gap-y-1.5 text-[12.5px] text-muted-foreground">
              <span>
                {stepCount} step{stepCount === 1 ? "" : "s"}
              </span>
              <DocLiveIndicator />
              <span className="flex-1" />
              <button
                ref={learnButtonRef}
                type="button"
                aria-expanded={panelOpen && panelTab === "learn"}
                onClick={() => {
                  panelOpener.current = learnButtonRef.current;
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
            <DocProposalProvider value={proposal}>
              <DocScope
                items={tree.items}
                byId={byId}
                scope={TOP_SCOPE}
                noteProblems={noteProblems}
              />
              <DocUnanchoredRemovals />
              {edit && (
                <div className="mt-3">
                  <DocAddStep
                    place={{
                      scope: TOP_SCOPE,
                      index: Number.MAX_SAFE_INTEGER,
                    }}
                    label="Add step"
                  />
                </div>
              )}
            </DocProposalProvider>
          </div>
        </div>
        {wide && !panelOpen && (
          <DocOutline items={tree.items} byId={byId} scrollRef={scrollRef} />
        )}
        {panelOpen && (
          <DocSidePanel
            tab={panelTab}
            onTabChange={setPanelTab}
            selectedStep={selectedStepId ? byId.get(selectedStepId) : undefined}
            editable={editable}
            learn={learn}
            onLearn={setLearn}
            onClose={closePanel}
          />
        )}
      </div>
    </WorkflowDocActionsContext.Provider>
  );
}
