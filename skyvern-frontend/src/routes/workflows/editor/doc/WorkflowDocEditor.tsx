import { useEffect, useMemo, useRef, useState } from "react";
import type { Edge } from "@xyflow/react";
import { QuestionMarkCircledIcon } from "@radix-ui/react-icons";
import { usePostHog } from "posthog-js/react";
import { cn } from "@/util/utils";
import { useWorkflowPresentationStore } from "@/store/WorkflowPresentationStore";
import { useWorkflowTitleStore } from "@/store/WorkflowTitleStore";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import {
  selectEditorMutationLocked,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";
import type { AppNode } from "../nodes";
import { useLocateBlockStore } from "../runValidation/useLocateBlockStore";
import {
  useWorkflowScopeId,
  useWorkflowScopeReadOnly,
} from "../WorkflowScopeContext";
import { buildDocTree, countDocSteps, type DocItem } from "./buildDocTree";
import { docLearnContent, type DocLearnContent } from "./docLearnContent";
import { getDocInputChips } from "./docInputChips";
import { WorkflowDocActionsContext } from "./WorkflowDocActionsContext";
import { DocOutline } from "./DocOutline";
import { DocScope } from "./DocScope";
import { DocSidePanel } from "./DocSidePanel";
import { DocText } from "./DocText";

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

export function WorkflowDocEditor({
  nodes,
  edges,
}: {
  nodes: AppNode[];
  edges: Edge[];
}) {
  const description = useWorkflowTitleStore((s) => s.description);
  const parameters = useWorkflowParametersStore((s) => s.parameters);
  const scopeReadOnly = useWorkflowScopeReadOnly();
  const locked = useWorkflowYamlEditorStore(selectEditorMutationLocked);
  const editable = !scopeReadOnly && !locked;
  const workflowId = useWorkflowScopeId();
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
  const byId = useMemo(
    () => new Map(nodes.map((node) => [node.id, node])),
    [nodes],
  );
  const inputs = useMemo(
    () => getDocInputChips(parameters, nodes),
    [parameters, nodes],
  );
  const stepCount = countDocSteps(tree.items);

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
    goToRef.current(locateRequest.nodeId);
    clearLocate();
  }, [locateRequest, clearLocate]);
  const openInputs = () =>
    useWorkflowPanelStore
      .getState()
      .setWorkflowPanelState({ active: true, content: "parameters" });

  return (
    <WorkflowDocActionsContext.Provider
      value={{
        editable,
        openSettings: (id) => {
          panelOpener.current = document.activeElement as HTMLElement;
          setSelectedStepId(id);
          setPanelTab("settings");
          setPanelOpen(true);
        },
        openVisual: () =>
          useWorkflowPresentationStore.getState().setPresentation("visual"),
        goTo,
        selectedBranch: (conditionalId) => branches[conditionalId],
        selectBranch: (conditionalId, branchId) =>
          setBranches((current) => ({ ...current, [conditionalId]: branchId })),
        loopOpen: (loopId) => !closedLoops.has(loopId),
        toggleLoop: (loopId) =>
          setClosedLoops((current) => {
            const next = new Set(current);
            if (!next.delete(loopId)) next.add(loopId);
            return next;
          }),
      }}
    >
      <div
        ref={rootRef}
        tabIndex={-1}
        role="region"
        aria-label="Workflow document"
        className="absolute inset-x-0 bottom-0 top-[var(--run-blocking-surface-bottom,0px)] z-30 flex min-w-0 bg-background text-foreground outline-none"
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
            <div className="mb-[26px] flex flex-wrap items-center gap-1.5 border-b border-border pb-[18px]">
              <span className="mr-1 text-[12.5px] text-muted-foreground">
                Inputs
              </span>
              {inputs.map((input) => (
                <button
                  key={input.key}
                  type="button"
                  onClick={openInputs}
                  title={
                    input.missing
                      ? "Needed to run"
                      : input.unused
                        ? "Not used by any step"
                        : undefined
                  }
                  className={cn(
                    "inline-flex max-w-full items-center gap-[5px] whitespace-nowrap rounded-md border border-border bg-background py-0.5 pl-[7px] pr-[9px] text-[13px] outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring",
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
                </button>
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
            {description?.trim() && (
              <p className="mb-3 text-base leading-[1.55] text-muted-foreground">
                <DocText value={description} />
              </p>
            )}
            <DocScope items={tree.items} byId={byId} />
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
