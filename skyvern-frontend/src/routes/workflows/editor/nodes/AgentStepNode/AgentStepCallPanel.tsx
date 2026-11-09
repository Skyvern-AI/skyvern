import { Cross2Icon } from "@radix-ui/react-icons";
import { ReactFlowProvider } from "@xyflow/react";
import {
  type KeyboardEvent,
  type SyntheticEvent,
  useEffect,
  useId,
  useMemo,
  useRef,
} from "react";
import { stringify as convertToYAML } from "yaml";

import { CopyButton } from "@/components/CopyButton";
import { PaneErrorBoundary } from "@/components/PaneErrorBoundary";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  type AgentStepTrailEntry,
  toolCallArgumentFields,
  toolCallBlock,
  toolCallTitle,
} from "@/routes/workflows/studio/agentStepTrail";
import {
  useAgentStepTrail,
  useSelectedAgentStepCall,
} from "@/routes/workflows/studio/useAgentStepTrail";
import { StatusDot } from "@/routes/workflows/workflowRun/StatusDot";
import { useLogging } from "@/hooks/useLogging";
import { useRunViewStore } from "@/store/RunViewStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { cn } from "@/util/utils";

import type { WorkflowBlock } from "../../../types/workflowTypes";
import type { BlockYAML } from "../../../types/workflowYamlTypes";
import {
  BLOCK_FORMS,
  type WorkflowBlockNodeType,
} from "../../panels/BlockConfigForm";
import { WorkflowScopeContext } from "../../WorkflowScopeContext";
import { convertToNode } from "../../workflowEditorUtils";
import { ToolCallIcon } from "./ToolCallIcon";

const CALL_NODE_ID = "agent-step-call";
const READ_ONLY_SCOPE = { workflowId: null, readOnly: true };
const CONTROLS =
  "button, select, input[type=number], [role=combobox], [role=switch], [role=checkbox], [role=radio]";
// Expanding a section such as Advanced Settings only shows more of the settings.
const DISCLOSURE =
  "button[aria-expanded][aria-controls]:not([role]):not([aria-haspopup])";

// Not every block form input reads the read-only scope, so edits and control presses are stopped here; text stays
// selectable, which inert or a disabled fieldset would prevent.
function stop(event: Event | SyntheticEvent) {
  event.preventDefault();
  event.stopPropagation();
}

function guardedControl(target: EventTarget | null): Element | null {
  const control = target instanceof Element ? target.closest(CONTROLS) : null;
  return control && !control.matches(DISCLOSURE) ? control : null;
}

function stopControl(event: SyntheticEvent) {
  if (guardedControl(event.target)) {
    stop(event);
  }
}

const TEXT_FIELDS =
  "input:not([type=number]), textarea, [contenteditable=true]";

function markGuardedControls(form: HTMLElement) {
  for (const control of form.querySelectorAll(CONTROLS)) {
    if (guardedControl(control)) {
      control.setAttribute("aria-disabled", "true");
    }
  }
  for (const field of form.querySelectorAll(TEXT_FIELDS)) {
    field.setAttribute("aria-readonly", "true");
    if (
      field instanceof HTMLInputElement ||
      field instanceof HTMLTextAreaElement
    ) {
      field.readOnly = true;
    }
  }
}

const READ_ONLY_HANDLERS = {
  onClickCapture: stopControl,
  onPointerDownCapture: stopControl,
  onKeyDownCapture: (event: KeyboardEvent) => {
    if (["Enter", " ", "ArrowDown", "ArrowUp"].includes(event.key)) {
      stopControl(event);
    }
  },
};

// The stored block is workflow YAML parsed from JSON. The form reads the API shape, which always carries the list and
// JSON fields YAML may omit; API-only fields such as output_parameter are never read by the form, so none are invented.
const YAML_OMITTED_DEFAULTS = {
  data_schema: null,
  json_schema: null,
  error_code_mapping: null,
  recipients: [],
  file_attachments: [],
};

function apiBlockFromYAML(block: BlockYAML): WorkflowBlock {
  const parsed: unknown = { ...YAML_OMITTED_DEFAULTS, ...block };
  return parsed as WorkflowBlock;
}

function ReadOnlyBlockSettings({ block }: { block: BlockYAML }) {
  const form = useRef<HTMLDivElement>(null);
  // React's onBeforeInput never sees deletions, pastes or drops; the native event covers every edit to text.
  useEffect(() => {
    const element = form.current;
    if (!element) {
      return;
    }
    element.addEventListener("beforeinput", stop, true);
    markGuardedControls(element);
    // Sections that expand later mount more controls.
    const observer = new MutationObserver(() => markGuardedControls(element));
    observer.observe(element, { childList: true, subtree: true });
    return () => {
      observer.disconnect();
      element.removeEventListener("beforeinput", stop, true);
    };
  }, []);
  const node = useMemo(() => {
    const converted = convertToNode(
      { id: CALL_NODE_ID },
      apiBlockFromYAML(block),
      false,
    );
    const keys = "parameter_keys" in block ? block.parameter_keys : null;
    return keys && "parameterKeys" in converted.data
      ? { ...converted, data: { ...converted.data, parameterKeys: keys } }
      : converted;
  }, [block]);
  const Form = BLOCK_FORMS[node.type as WorkflowBlockNodeType];
  if (!Form) {
    return null;
  }
  return (
    <WorkflowScopeContext.Provider value={READ_ONLY_SCOPE}>
      <ReactFlowProvider initialNodes={[node]}>
        <div
          ref={form}
          className="min-w-0 [&_input::placeholder]:text-transparent [&_input]:cursor-default [&_textarea::placeholder]:text-transparent [&_textarea]:cursor-default"
          {...READ_ONLY_HANDLERS}
        >
          <Form blockId={CALL_NODE_ID} />
        </div>
      </ReactFlowProvider>
    </WorkflowScopeContext.Provider>
  );
}

function ArgumentFields({ entry }: { entry: AgentStepTrailEntry }) {
  const fields = toolCallArgumentFields(entry.call);
  if (fields.length === 0) {
    return <p className="text-sm text-muted-foreground">No arguments.</p>;
  }
  return (
    <dl className="space-y-4">
      {fields.map(([name, value]) => (
        <div key={name} className="space-y-2">
          <dt className="text-xs font-medium text-foreground dark:text-slate-300">
            {name}
          </dt>
          <dd>
            <pre className="whitespace-pre-wrap break-words rounded-md bg-slate-elevation5 p-3 text-xs">
              {value}
            </pre>
          </dd>
        </div>
      ))}
    </dl>
  );
}

function CallOutput({ entry }: { entry: AgentStepTrailEntry }) {
  const child = entry.child?.block;
  if (!child) {
    const status = entry.running
      ? "running"
      : (entry.call.status ?? entry.status);
    return (
      <div className="flex items-center gap-2 text-sm">
        <StatusDot status={entry.status} isFinalized={!entry.running} />
        <span className="text-muted-foreground">
          {status.charAt(0).toUpperCase() + status.slice(1).replace(/_/g, " ")}
        </span>
      </div>
    );
  }
  const failed = child.output == null && Boolean(child.failure_reason);
  const output = failed ? child.failure_reason : (child.output ?? null);
  return (
    <pre
      className={cn(
        "whitespace-pre-wrap break-words rounded-md bg-slate-elevation5 p-3 text-xs",
        failed && "text-destructive",
      )}
    >
      {output === null || output === undefined
        ? "No output yet."
        : typeof output === "string"
          ? output
          : JSON.stringify(output, null, 2)}
    </pre>
  );
}

function SelectedCallPanel({
  workflowRunBlockId,
  index,
}: {
  workflowRunBlockId: string;
  index: number;
}) {
  const trail = useAgentStepTrail({ workflowRunBlockId });
  const clear = useRunViewStore((s) => s.clearAgentStepCall);
  const logging = useLogging();
  const titleId = useId();
  const region = useRef<HTMLElement>(null);
  const opener = useRef(
    document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null,
  );
  useEffect(() => {
    region.current?.focus();
    const returnTo = opener.current;
    return () => {
      if (returnTo?.isConnected) {
        returnTo.focus();
      }
    };
  }, []);
  const entry = trail?.entries.find((entry) => entry.call.index === index);
  const blockJson = entry?.call.block;
  const block = useMemo(() => toolCallBlock(blockJson), [blockJson]);
  if (!entry) {
    return null;
  }
  const title = `#${entry.call.index} ${toolCallTitle(entry.call)}`;

  return (
    <section
      ref={region}
      tabIndex={-1}
      aria-labelledby={titleId}
      onKeyDown={(event) => {
        if (event.key === "Escape" && !event.defaultPrevented) {
          clear();
        }
      }}
      className="absolute bottom-6 right-6 top-8 z-40 flex w-[30rem] max-w-[calc(100%-3rem)] flex-col overflow-hidden rounded-xl border border-border bg-slate-elevation2 shadow-xl outline-none"
    >
      <header className="flex h-20 shrink-0 items-center justify-between gap-3 border-b border-border px-6">
        <div className="flex min-w-0 items-center gap-3">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded border border-border dark:border-slate-600">
            <ToolCallIcon
              tool={entry.call.tool}
              blockType={entry.call.block_type}
              className="size-4"
            />
          </div>
          <div className="min-w-0">
            <h2
              id={titleId}
              className="truncate text-sm font-medium text-foreground"
            >
              {title}
            </h2>
            <p className="text-xs text-muted-foreground">Read only</p>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <StatusDot status={entry.status} isFinalized={!entry.running} />
          {block && (
            <TooltipProvider delayDuration={200}>
              <Tooltip>
                <TooltipTrigger asChild>
                  <span>
                    <CopyButton
                      value={convertToYAML(block)}
                      ariaLabel="Copy block YAML"
                    />
                  </span>
                </TooltipTrigger>
                <TooltipContent>Copy block YAML</TooltipContent>
              </Tooltip>
            </TooltipProvider>
          )}
          <button
            type="button"
            onClick={clear}
            aria-label="Close call settings"
            className="rounded p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground dark:hover:bg-slate-700"
          >
            <Cross2Icon className="h-4 w-4" />
          </button>
        </div>
      </header>
      <Tabs
        defaultValue="settings"
        className="flex-1 overflow-y-auto px-6 py-5"
      >
        <TabsList>
          <TabsTrigger value="settings">Settings</TabsTrigger>
          <TabsTrigger value="output">Output</TabsTrigger>
        </TabsList>
        <TabsContent value="settings" className="pt-2">
          {block ? (
            <PaneErrorBoundary
              key={`${workflowRunBlockId}-${index}`}
              pane="agent-step-call"
              fallback={<ArgumentFields entry={entry} />}
              onError={(error, componentStack) =>
                logging.error("Agent step call settings crashed", {
                  error_source: "agent_step_call_panel",
                  block_type: entry.call.block_type,
                  component_stack: componentStack,
                  error,
                })
              }
            >
              <ReadOnlyBlockSettings block={block} />
            </PaneErrorBoundary>
          ) : (
            <ArgumentFields entry={entry} />
          )}
        </TabsContent>
        <TabsContent value="output" className="pt-2">
          <CallOutput entry={entry} />
        </TabsContent>
      </Tabs>
    </section>
  );
}

function AgentStepCallPanel() {
  const selected = useSelectedAgentStepCall();
  const clear = useRunViewStore((s) => s.clearAgentStepCall);
  const selectedBlockId = useWorkflowPanelStore((s) => s.selectedBlockId);
  const libraryOpen = useWorkflowPanelStore(
    (s) =>
      s.workflowPanelState.active &&
      s.workflowPanelState.content === "nodeLibrary",
  );
  // Selecting a block or opening the block library opens its own panel, so only one panel shows at a time.
  useEffect(() => {
    if (selectedBlockId !== null || libraryOpen) {
      clear();
    }
  }, [selectedBlockId, libraryOpen, clear]);
  if (!selected || selectedBlockId !== null) {
    return null;
  }
  return (
    <SelectedCallPanel
      key={`${selected.workflowRunBlockId}-${selected.index}`}
      workflowRunBlockId={selected.workflowRunBlockId}
      index={selected.index}
    />
  );
}

export { AgentStepCallPanel };
