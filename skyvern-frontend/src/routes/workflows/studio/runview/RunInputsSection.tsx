import { CopyButton } from "@/components/CopyButton";
import { workflowBlockTitle } from "@/routes/workflows/editor/nodes/types";

import type { WorkflowParameter } from "../../types/workflowTypes";
import type { BlockPrompt } from "./blockPrompts";
import { OverviewField } from "./OverviewField";
import { ClampedProse, RunFieldValue } from "./RunFieldValue";

export type RunInputMeta = { label: string; value: string };

type RunInputsSectionProps = {
  // Ordered [key, value, definition] entries for the agent (workflow) inputs this run used.
  parameters: Array<[string, unknown, WorkflowParameter?]>;
  blockPrompts: BlockPrompt[];
  // Run-level non-parameter inputs (webhook, proxy, headers, …).
  meta: RunInputMeta[];
};

export function RunInputsSection({
  parameters,
  blockPrompts,
  meta,
}: RunInputsSectionProps) {
  if (
    parameters.length === 0 &&
    blockPrompts.length === 0 &&
    meta.length === 0
  ) {
    return null;
  }

  return (
    <div className="flex flex-col gap-6">
      {parameters.length > 0 ? (
        <div className="flex flex-col gap-2">
          <span className="text-xs font-medium text-muted-foreground">
            Run inputs
          </span>
          <div className="flex flex-col gap-4">
            {parameters.map(([key, value, definition]) => (
              <OverviewField key={key} label={key}>
                <div className="flex flex-col gap-1.5">
                  {definition?.description ? (
                    <span className="text-xs text-muted-foreground">
                      {definition.description}
                    </span>
                  ) : null}
                  <RunFieldValue value={value} label={key} />
                </div>
              </OverviewField>
            ))}
          </div>
        </div>
      ) : null}
      {blockPrompts.length > 0 ? (
        <div className="flex flex-col gap-3">
          <span className="text-xs font-medium text-muted-foreground">
            Block prompts
          </span>
          <div className="flex flex-col gap-[18px]">
            {blockPrompts.map((block, index) => {
              const typeLabel = workflowBlockTitle[block.blockType];
              return (
                <div
                  // Block labels are only softly unique across loop scopes, so
                  // pair with the flattened index to keep keys stable.
                  key={`${block.blockLabel}-${index}`}
                >
                  <div className="mb-2.5 text-sm font-medium text-foreground">
                    {block.blockLabel}
                    {typeLabel ? (
                      <span className="ml-2 font-normal text-muted-foreground">
                        {typeLabel}
                      </span>
                    ) : null}
                  </div>
                  <div className="flex flex-col gap-3">
                    {block.fields.map((field) => (
                      <div key={field.fieldLabel} className="flex flex-col">
                        <span className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                          {field.fieldLabel}
                        </span>
                        <ClampedProse text={field.prompt} />
                      </div>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ) : null}
      {meta.length > 0 ? (
        <div className="flex flex-col gap-3">
          <span className="text-xs font-medium text-muted-foreground">
            Other inputs
          </span>
          <div className="overflow-hidden rounded-lg border border-border bg-slate-elevation2">
            {meta.map((entry) => (
              <div
                key={entry.label}
                className="group flex items-start gap-3 border-t border-border px-3 py-[9px] first:border-t-0"
              >
                <span className="w-32 shrink-0 pt-px text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                  {entry.label}
                </span>
                <span className="min-w-0 flex-1 break-words text-sm text-foreground">
                  {entry.value}
                </span>
                <CopyButton
                  value={entry.value}
                  className="h-6 w-6 shrink-0 text-muted-foreground opacity-0 transition-opacity hover:text-foreground focus-visible:opacity-100 group-hover:opacity-100"
                />
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
