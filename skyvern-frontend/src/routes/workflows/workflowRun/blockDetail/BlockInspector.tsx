import { ChevronDownIcon, ChevronRightIcon } from "@radix-ui/react-icons";
import { useEffect, useMemo, useState } from "react";

import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Status } from "@/api/types";
import { cn } from "@/util/utils";
import {
  hasExtractedInformation,
  type WorkflowRunBlock,
} from "../../types/workflowRunTypes";
import { isTaskVariantBlock } from "../../types/workflowTypes";

type InspectorField = {
  label: string;
  value: unknown;
  kind?: "json" | "text";
};

type JsonExplorerProps = {
  value: unknown;
  rootLabel?: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isEmptyValue(value: unknown): boolean {
  if (value === null || value === undefined || value === "") return true;
  if (Array.isArray(value)) return value.length === 0;
  if (isRecord(value)) return Object.keys(value).length === 0;
  return false;
}

function valueMatchesSearch(value: unknown, search: string): boolean {
  if (!search) return true;
  if (Array.isArray(value)) {
    return value.some((child) => valueMatchesSearch(child, search));
  }
  if (isRecord(value)) {
    return Object.entries(value).some(
      ([key, childValue]) =>
        key.toLowerCase().includes(search) ||
        valueMatchesSearch(childValue, search),
    );
  }
  return primitivePreview(value).toLowerCase().includes(search);
}

function nodeOwnTextMatchesSearch(
  label: string,
  value: unknown,
  search: string,
) {
  if (!search) return false;
  if (label.toLowerCase().includes(search)) return true;
  if (Array.isArray(value) || isRecord(value)) return false;
  return primitivePreview(value).toLowerCase().includes(search);
}

function HighlightedText({ text, search }: { text: string; search: string }) {
  if (!search) return <>{text}</>;

  const lowerText = text.toLowerCase();
  const parts: Array<{ text: string; matched: boolean }> = [];
  let cursor = 0;
  let matchIndex = lowerText.indexOf(search, cursor);

  while (matchIndex !== -1) {
    if (matchIndex > cursor) {
      parts.push({ text: text.slice(cursor, matchIndex), matched: false });
    }
    const matchEnd = matchIndex + search.length;
    parts.push({ text: text.slice(matchIndex, matchEnd), matched: true });
    cursor = matchEnd;
    matchIndex = lowerText.indexOf(search, cursor);
  }

  if (cursor < text.length) {
    parts.push({ text: text.slice(cursor), matched: false });
  }

  return (
    <>
      {parts.map((part, index) =>
        part.matched ? (
          <mark
            key={index}
            className="rounded bg-amber-300/20 px-0.5 font-medium text-amber-100 ring-1 ring-amber-200/20"
          >
            {part.text}
          </mark>
        ) : (
          <span key={index}>{part.text}</span>
        ),
      )}
    </>
  );
}

function FieldValue({ field }: { field: InspectorField }) {
  if (field.kind === "json" || typeof field.value === "object") {
    return <JsonExplorer value={field.value} rootLabel={field.label} />;
  }
  return (
    <div className="whitespace-pre-wrap break-words rounded bg-slate-elevation1 px-2.5 py-2 text-xs text-slate-300">
      {String(field.value)}
    </div>
  );
}

function FieldList({
  fields,
  emptyText,
}: {
  fields: Array<InspectorField>;
  emptyText: string;
}) {
  if (fields.length === 0) {
    return <div className="text-xs text-slate-500">{emptyText}</div>;
  }
  return (
    <div className="space-y-3">
      {fields.map((field) => (
        <div key={field.label} className="space-y-1.5">
          <div className="text-[11px] font-medium text-slate-500">
            {field.label}
          </div>
          <FieldValue field={field} />
        </div>
      ))}
    </div>
  );
}

function primitivePreview(value: unknown): string {
  if (typeof value === "string") return `"${value}"`;
  if (value === null) return "null";
  return String(value);
}

function truncatePreview(value: string, maxLength = 72): string {
  if (value.length <= maxLength) return value;
  return `${value.slice(0, maxLength - 1)}…`;
}

function objectPreview(value: Record<string, unknown>): string {
  const entries = Object.entries(value);
  if (entries.length === 0) return "{}";

  const preview = entries
    .slice(0, 3)
    .map(([key, childValue]) => `${key}: ${compactValuePreview(childValue)}`)
    .join(", ");
  return `{ ${preview}${entries.length > 3 ? ", ..." : ""} }`;
}

function compactValuePreview(value: unknown): string {
  if (Array.isArray(value)) {
    return `${value.length} ${value.length === 1 ? "entry" : "entries"}`;
  }
  if (isRecord(value)) return objectPreview(value);
  return primitivePreview(value);
}

function expandablePreview(value: unknown, childCount: number): string {
  if (Array.isArray(value)) {
    const preview = value
      .slice(0, 3)
      .map((child) => compactValuePreview(child))
      .join(", ");
    return truncatePreview(
      `${childCount} ${childCount === 1 ? "entry" : "entries"}${preview ? ` [${preview}]` : ""}`,
    );
  }

  if (isRecord(value)) {
    if (childCount === 0) return "{}";
    const preview = Object.entries(value)
      .slice(0, 3)
      .map(([key, childValue]) => `${key}: ${compactValuePreview(childValue)}`)
      .join(", ");
    return truncatePreview(`{ ${preview}${childCount > 3 ? ", ..." : ""} }`);
  }

  return compactValuePreview(value);
}

function JsonNode({
  label,
  value,
  path,
  search,
  expanded,
  onToggle,
}: {
  label: string;
  value: unknown;
  path: string;
  search: string;
  expanded: ReadonlySet<string>;
  onToggle: (path: string) => void;
}) {
  const isArray = Array.isArray(value);
  const isObject = isRecord(value);
  const isExpandable = isArray || isObject;
  const children = isArray
    ? value.map((child, index) => [String(index), child] as const)
    : isObject
      ? Object.entries(value)
      : [];
  const hasSearch = search.length > 0;
  const ownMatches = nodeOwnTextMatchesSearch(label, value, search);
  const descendantMatches =
    (isArray || isObject) && valueMatchesSearch(value, search);
  const matches = ownMatches || descendantMatches;
  if (hasSearch && !matches) return null;

  const isOpen = hasSearch || expanded.has(path);
  const childCount = children.length;

  if (!isExpandable) {
    return (
      <div className="flex min-w-0 items-start gap-1 py-0.5 text-xs">
        <span className="size-3.5 shrink-0" aria-hidden="true" />
        <span className="shrink-0 text-slate-500">
          <HighlightedText text={label} search={search} />
        </span>
        <span className="min-w-0 break-words font-mono text-slate-300">
          <HighlightedText text={primitivePreview(value)} search={search} />
        </span>
      </div>
    );
  }

  return (
    <div className="min-w-0 text-xs">
      <button
        type="button"
        onClick={() => onToggle(path)}
        className="flex w-full min-w-0 cursor-pointer items-center gap-1 rounded py-0.5 text-left outline-none hover:bg-slate-800/60 focus-visible:ring-1 focus-visible:ring-white/40"
      >
        {isOpen ? (
          <ChevronDownIcon className="size-3.5 shrink-0 text-slate-500" />
        ) : (
          <ChevronRightIcon className="size-3.5 shrink-0 text-slate-500" />
        )}
        <span className="shrink-0 text-slate-400">
          <HighlightedText text={label} search={search} />
        </span>
        {!isOpen && (
          <span className="min-w-0 truncate font-mono text-slate-300">
            {expandablePreview(value, childCount)}
          </span>
        )}
      </button>
      {isOpen && (
        <div className="ml-4 border-l border-slate-700 pl-3">
          {children.map(([childKey, childValue]) => (
            <JsonNode
              key={`${path}.${childKey}`}
              label={isArray ? `[${childKey}]` : childKey}
              value={childValue}
              path={`${path}.${childKey}`}
              search={search}
              expanded={expanded}
              onToggle={onToggle}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function JsonExplorer({ value, rootLabel = "value" }: JsonExplorerProps) {
  const [search, setSearch] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set(["$"]));
  const normalizedSearch = search.trim().toLowerCase();
  const rootIsExpandable = Array.isArray(value) || isRecord(value);

  function toggle(path: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });
  }

  return (
    <div className="space-y-2 rounded bg-slate-elevation1 p-2">
      {rootIsExpandable && (
        <Input
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Search JSON"
          className="h-7 border-slate-700 bg-slate-elevation2 text-xs"
        />
      )}
      <div className="max-h-80 overflow-auto rounded bg-slate-elevation2 p-2">
        <JsonNode
          label={rootLabel}
          value={value}
          path="$"
          search={normalizedSearch}
          expanded={expanded}
          onToggle={toggle}
        />
      </div>
    </div>
  );
}

function pushField(
  fields: Array<InspectorField>,
  label: string,
  value: unknown,
  kind?: InspectorField["kind"],
) {
  if (isEmptyValue(value)) return;
  fields.push({ label, value, kind });
}

function getInputFields(block: WorkflowRunBlock): Array<InspectorField> {
  const fields: Array<InspectorField> = [];
  pushField(fields, "Description", block.description);
  pushField(fields, "Prompt", block.prompt);
  pushField(fields, "URL", block.url);
  pushField(fields, "Navigation goal", block.navigation_goal);
  pushField(fields, "Extraction goal", block.data_extraction_goal);
  pushField(fields, "Data schema", block.data_schema, "json");
  pushField(fields, "Complete criterion", block.complete_criterion);
  pushField(fields, "Terminate criterion", block.terminate_criterion);
  pushField(fields, "Loop values", block.loop_values, "json");
  pushField(fields, "Current value", block.current_value);
  pushField(fields, "Instructions", block.instructions);
  pushField(fields, "Subject", block.subject);
  pushField(fields, "Recipients", block.recipients, "json");
  pushField(fields, "Body", block.body);
  pushField(fields, "Wait seconds", block.wait_sec);
  pushField(fields, "HTTP method", block.method);
  pushField(fields, "Headers", block.headers, "json");
  pushField(fields, "Request body", block.request_body, "json");
  pushField(fields, "Continue on failure", block.continue_on_failure);
  return fields;
}

function getOutputValue(block: WorkflowRunBlock): unknown {
  if (
    isTaskVariantBlock(block) &&
    block.status === Status.Completed &&
    hasExtractedInformation(block.output)
  ) {
    return block.output.extracted_information;
  }
  return block.output;
}

function getSummaryFields(block: WorkflowRunBlock): Array<InspectorField> {
  const fields: Array<InspectorField> = [];
  pushField(fields, "Task ID", block.task_id);
  pushField(fields, "Engine", block.engine);
  pushField(fields, "Failure reason", block.failure_reason);
  pushField(fields, "Executed branch", block.executed_branch_expression);
  pushField(fields, "Executed branch result", block.executed_branch_result);
  pushField(fields, "Executed next block", block.executed_branch_next_block);
  return fields;
}

function BlockInspector({ block }: { block: WorkflowRunBlock }) {
  const inputFields = useMemo(() => getInputFields(block), [block]);
  const summaryFields = useMemo(() => getSummaryFields(block), [block]);
  const outputValue = getOutputValue(block);
  const hasOutput = !isEmptyValue(outputValue);
  const defaultTab = hasOutput ? "outputs" : "summary";
  const [activeTab, setActiveTab] = useState(defaultTab);
  const triggerClassName =
    "rounded px-2.5 py-1 text-xs font-medium text-slate-400 transition-colors hover:bg-white/5 hover:text-slate-200 data-[state=active]:bg-slate-elevation4 data-[state=active]:text-slate-50 data-[state=active]:shadow-sm";

  useEffect(() => {
    setActiveTab(defaultTab);
  }, [block.workflow_run_block_id, defaultTab]);

  return (
    <div className="border-b border-slate-700 bg-slate-elevation1 px-3 py-3">
      <Tabs
        value={activeTab}
        onValueChange={setActiveTab}
        className="space-y-3"
      >
        <TabsList className="h-8 gap-0.5 rounded-md bg-slate-elevation2 p-0.5 ring-1 ring-inset ring-slate-700/60">
          <TabsTrigger className={triggerClassName} value="summary">
            Summary
          </TabsTrigger>
          <TabsTrigger className={triggerClassName} value="inputs">
            Inputs
          </TabsTrigger>
          <TabsTrigger
            className={cn(
              triggerClassName,
              !hasOutput && "cursor-not-allowed opacity-50",
            )}
            disabled={!hasOutput}
            value="outputs"
          >
            Outputs
          </TabsTrigger>
        </TabsList>
        <TabsContent value="summary" className="m-0">
          <FieldList
            fields={summaryFields}
            emptyText="No additional summary data."
          />
        </TabsContent>
        <TabsContent value="inputs" className="m-0">
          <FieldList fields={inputFields} emptyText="No block inputs found." />
        </TabsContent>
        <TabsContent value="outputs" className="m-0">
          {hasOutput ? (
            <JsonExplorer value={outputValue} rootLabel="output" />
          ) : (
            <div className="text-xs text-slate-500">No block output.</div>
          )}
        </TabsContent>
      </Tabs>
    </div>
  );
}

export { BlockInspector, JsonExplorer };
