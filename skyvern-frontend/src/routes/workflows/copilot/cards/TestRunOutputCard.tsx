import React, { useState } from "react";
import {
  ChevronRightIcon,
  CrossCircledIcon,
  ExternalLinkIcon,
} from "@radix-ui/react-icons";

import { Status } from "@/api/types";
import type {
  WorkflowApiResponse,
  WorkflowBlock,
} from "@/routes/workflows/types/workflowTypes";
import { visitWorkflowBlocks } from "../../workflowBlockUtils";
import { humanizeBlockLabel } from "../blockLabel";
import { TURN_ROW_INSET } from "./cardLayout";
import type { CopilotProposalRunFacts } from "../workflowCopilotTypes";

type RunOutput = CopilotProposalRunFacts["outputs"][number];

function isEmptyValue(value: unknown): boolean {
  if (value === null || value === undefined || value === "") return true;
  if (Array.isArray(value)) return value.length === 0;
  if (typeof value === "object") return Object.keys(value).length === 0;
  return false;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function renderOutputValue(value: unknown): React.ReactNode {
  if (isEmptyValue(value)) {
    return <span>—</span>;
  }
  if (typeof value === "string") {
    return (
      <span className="whitespace-pre-wrap [overflow-wrap:anywhere]">
        {value}
      </span>
    );
  }
  if (Array.isArray(value)) {
    return (
      <div className="ml-3">
        {value.map((item, index) => (
          <div key={index}>
            <span className="font-medium">{index + 1}.</span>{" "}
            {renderOutputValue(item)}
          </div>
        ))}
      </div>
    );
  }
  if (isRecord(value)) {
    return (
      <div className="ml-3">
        {Object.entries(value).map(([key, item]) => (
          <div key={key}>
            <span className="font-medium">{key}:</span>{" "}
            {renderOutputValue(item)}
          </div>
        ))}
      </div>
    );
  }
  return <span>{String(value)}</span>;
}

// A task block's output wraps its extraction (an object, list, or string) in
// bookkeeping such as ids and artifact lists; the extraction is the result.
function resultFields(value: unknown): Array<[string, unknown]> {
  if (isRecord(value) && "extracted_information" in value) {
    value = value.extracted_information;
  }
  if (!isRecord(value)) {
    return isEmptyValue(value) ? [] : [["result", value]];
  }
  return Object.entries(value).filter(([, item]) => !isEmptyValue(item));
}

function fieldName(key: string): string {
  const words = key.replace(/_/g, " ").trim();
  return words.length === 0 ? key : words[0]!.toUpperCase() + words.slice(1);
}

function Fields({
  fields,
  emphasized,
}: {
  fields: Array<[string, unknown]>;
  emphasized: boolean;
}) {
  return (
    <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
      {fields.map(([key, item]) => (
        <React.Fragment key={key}>
          <dt className="text-muted-foreground">{fieldName(key)}</dt>
          <dd
            className={`min-w-0 font-mono [overflow-wrap:anywhere] ${
              emphasized ? "text-foreground" : "text-muted-foreground"
            }`}
          >
            {renderOutputValue(item)}
          </dd>
        </React.Fragment>
      ))}
    </dl>
  );
}

function RawOutput({ outputs }: { outputs: RunOutput[] }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        className="flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground"
        onClick={() => setOpen((value) => !value)}
      >
        <ChevronRightIcon
          className={`h-3 w-3 transition-transform ${open ? "rotate-90" : ""}`}
        />
        Full run output
      </button>
      {open ? (
        <div className="mt-1.5 space-y-1 text-xs text-muted-foreground">
          {outputs.map((output) => (
            <div key={output.output_parameter_id}>
              <span className="font-medium text-foreground">
                {output.output_parameter_id}:
              </span>{" "}
              {renderOutputValue(output.value)}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function TestRunOutputCard({
  facts,
  workflow,
}: {
  facts: CopilotProposalRunFacts;
  workflow: WorkflowApiResponse | null;
}) {
  if (!facts.available) {
    return (
      <p
        className={`${TURN_ROW_INSET} text-xs text-muted-foreground`}
        data-testid="proposal-run-facts"
      >
        Associated test run unavailable. No other run was substituted.
      </p>
    );
  }

  const blocks: WorkflowBlock[] = [];
  visitWorkflowBlocks(workflow?.workflow_definition.blocks ?? [], (block) => {
    blocks.push(block);
  });
  const blockOrder = (output: RunOutput) => {
    const index = blocks.findIndex(
      (block) =>
        block.output_parameter?.output_parameter_id ===
        output.output_parameter_id,
    );
    return index === -1 ? blocks.length : index;
  };
  const sections = facts.outputs
    .map((output, arrival) => ({
      output,
      arrival,
      order: blockOrder(output),
      fields: resultFields(output.value),
    }))
    .filter((section) => section.fields.length > 0)
    .sort((a, b) => a.order - b.order || a.arrival - b.arrival);
  // A cancel, termination or timeout is not a failure the run found, so only Failed is marked.
  const failed = facts.status === Status.Failed;
  const final = sections[sections.length - 1];
  const earlier = sections.slice(0, -1);
  const blockName = (order: number) => {
    const block = blocks[order];
    return block === undefined ? null : humanizeBlockLabel(block.label);
  };

  return (
    <div
      className="overflow-hidden rounded-[10px] border border-border bg-slate-elevation2"
      data-testid="proposal-run-facts"
    >
      <div className="flex items-center gap-2 px-3 pt-3 text-xs font-semibold text-foreground">
        {failed ? (
          <CrossCircledIcon className="h-3.5 w-3.5 text-destructive" />
        ) : null}
        Test run output
        {facts.status === Status.Completed ? null : (
          <span className="font-normal text-muted-foreground">
            {` · ${facts.status ?? "status unavailable"}`}
          </span>
        )}
        <a
          className="ml-auto flex items-center gap-1 text-[11px] font-normal text-muted-foreground hover:text-foreground"
          href={`/runs/${facts.workflow_run_id}`}
          target="_blank"
          rel="noopener noreferrer"
        >
          View run <ExternalLinkIcon className="h-3 w-3" />
        </a>
      </div>
      <div className="space-y-3 px-3 pb-3 pt-2">
        {facts.failure_reason ? (
          <p className="whitespace-pre-wrap text-xs text-destructive [overflow-wrap:anywhere]">
            {facts.failure_reason}
          </p>
        ) : null}
        {final ? (
          <div className="rounded-md border border-border bg-slate-elevation3 p-2.5">
            {blockName(final.order) ? (
              <div className="mb-1.5 text-[10px] font-bold uppercase tracking-wide text-muted-foreground">
                {blockName(final.order)}
              </div>
            ) : null}
            <Fields fields={final.fields} emphasized />
          </div>
        ) : null}
        {earlier.map((section) => (
          <div key={section.output.output_parameter_id} className="px-0.5">
            {blockName(section.order) ? (
              <div className="mb-1 text-[10px] font-bold uppercase tracking-wide text-muted-foreground">
                {blockName(section.order)}
              </div>
            ) : null}
            <Fields fields={section.fields} emphasized={false} />
          </div>
        ))}
        {facts.outputs.length > 0 ? (
          <RawOutput outputs={facts.outputs} />
        ) : null}
      </div>
    </div>
  );
}
