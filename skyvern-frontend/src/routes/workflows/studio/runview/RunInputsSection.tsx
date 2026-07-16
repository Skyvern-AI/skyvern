import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { defaultSettingsTokyoNightStorm } from "@uiw/codemirror-theme-tokyo-night-storm";

import { CopyButton } from "@/components/CopyButton";
import { Button } from "@/components/ui/button";
import { workflowBlockTitle } from "@/routes/workflows/editor/nodes/types";

import type { BlockPrompt } from "./blockPrompts";
import { OverviewCodeBlock } from "./OverviewCodeBlock";

export type RunInputMeta = { label: string; value: string };

type RunInputsSectionProps = {
  // Ordered [key, value] entries for the agent (workflow) inputs this run used.
  parameters: Array<[string, unknown]>;
  blockPrompts: BlockPrompt[];
  // Run-level non-parameter inputs (webhook, proxy, headers, …).
  meta: RunInputMeta[];
};

// The background/foreground CodeMirror paints for the JSON Agent-inputs block
// (tokyoNightStorm). Prompts are plain text, so this inset reproduces the
// OverviewCodeBlock chrome without mounting a CodeMirror per prompt.
const CODE_INSET_BG = defaultSettingsTokyoNightStorm.background ?? "#24283b";
const CODE_INSET_FG = defaultSettingsTokyoNightStorm.foreground ?? "#7982a9";
// Collapsed height shows ~4 lines; render a few past that so the clamp is filled,
// but not the whole prompt — an unbounded paste would otherwise build thousands
// of hidden line nodes.
const COLLAPSED_LINE_CAP = 6;

function PromptCodeInset({ prompt }: { prompt: string }) {
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const contentId = useId();
  const boxRef = useRef<HTMLDivElement>(null);
  const measureOverflow = useCallback(() => {
    const element = boxRef.current;
    if (element) {
      setOverflows(element.scrollHeight > element.clientHeight);
    }
  }, []);

  useLayoutEffect(() => {
    if (!expanded) {
      measureOverflow();
    }
  }, [expanded, measureOverflow, prompt]);

  useEffect(() => {
    if (expanded || typeof ResizeObserver === "undefined") {
      return;
    }
    const element = boxRef.current;
    if (!element) {
      return;
    }
    // Width changes (pane resize) re-wrap the prompt and can flip the overflow
    // verdict even though the collapsed height is pinned — so keep observing.
    const observer = new ResizeObserver(measureOverflow);
    observer.observe(element);
    return () => observer.disconnect();
  }, [expanded, measureOverflow]);

  const lines = prompt.split("\n");
  const visibleLines = expanded ? lines : lines.slice(0, COLLAPSED_LINE_CAP);
  const showToggle = overflows || lines.length > COLLAPSED_LINE_CAP;

  return (
    <div className="flex flex-col items-start">
      <div className="relative w-full">
        <CopyButton
          value={prompt}
          className="absolute right-2 top-2 z-10 h-7 w-7 bg-slate-elevation3/80 text-muted-foreground backdrop-blur hover:bg-slate-elevation4 hover:text-foreground"
        />
        <div
          id={contentId}
          ref={boxRef}
          style={{ backgroundColor: CODE_INSET_BG, color: CODE_INSET_FG }}
          // ~3 lines at the inset's 12px/1.4; a height clamp (not line-clamp-3)
          // so the per-line number gutter rows stay intact.
          className={`overflow-hidden rounded-md py-1 font-mono text-xs leading-[1.4] ${expanded ? "" : "max-h-[66px]"}`}
        >
          {visibleLines.map((line, index) => (
            <div key={index} className="flex items-start">
              <span className="min-w-[22px] shrink-0 select-none whitespace-nowrap pl-[5px] pr-2 text-right">
                {index + 1}
              </span>
              {/* pr-10 clears the absolute copy button so a full-width wrapped
                  first line isn't occluded. */}
              <span className="min-w-0 flex-1 whitespace-pre-wrap break-words pl-1.5 pr-10">
                {line === "" ? " " : line}
              </span>
            </div>
          ))}
        </div>
        {!expanded && showToggle ? (
          <div
            aria-hidden
            className="pointer-events-none absolute inset-x-0 bottom-0 h-[26px] rounded-b-md"
            style={{
              background: `linear-gradient(to bottom, transparent, ${CODE_INSET_BG})`,
            }}
          />
        ) : null}
      </div>
      {showToggle ? (
        <Button
          type="button"
          variant="link"
          size="sm"
          className="mt-1.5 h-auto justify-start p-0 text-xs text-muted-foreground hover:text-foreground"
          aria-controls={contentId}
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "Show less" : "Show more"}
        </Button>
      ) : null}
    </div>
  );
}

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

  const agentInputs = Object.fromEntries(parameters);

  return (
    <div className="flex flex-col gap-6">
      {parameters.length > 0 ? (
        <div className="flex flex-col gap-2">
          <span className="text-xs font-medium text-muted-foreground">
            Agent inputs
          </span>
          <OverviewCodeBlock
            value={JSON.stringify(agentInputs, null, 2)}
            maxHeight="320px"
          />
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
                  <div className="mb-2.5 font-mono text-sm font-medium text-foreground">
                    {block.blockLabel}
                    {typeLabel ? (
                      <span className="ml-2 font-sans font-normal text-muted-foreground">
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
                        <PromptCodeInset prompt={field.prompt} />
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
