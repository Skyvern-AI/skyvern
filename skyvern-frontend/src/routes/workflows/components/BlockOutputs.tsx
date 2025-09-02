import { useState } from "react";
import { useParams } from "react-router-dom";

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { SwitchBar } from "@/components/SwitchBar";
import { useBlockOutputStore } from "@/store/BlockOutputStore";
import { cn, formatMs } from "@/util/utils";

import { CodeEditor } from "./CodeEditor";

type PageName = "output" | "override";

function BlockOutputs({
  blockLabel,
  blockOutput,
}: {
  blockLabel: string;
  blockOutput: { [k: string]: unknown } | null;
}) {
  const { workflowPermanentId } = useParams();
  const blockOutputStore = useBlockOutputStore();
  const [pageName, setPageName] = useState<PageName>("output");
  const [overrideHasError, setOverrideHasError] = useState(false);
  const useOverride = useBlockOutputStore((state) =>
    workflowPermanentId
      ? state.useOverrides[workflowPermanentId]?.[blockLabel] ?? false
      : false,
  );

  let createdAt: Date | null = null;

  if (blockOutput) {
    delete blockOutput.task_id;
    delete blockOutput.status;
    delete blockOutput.failure_reason;
    delete blockOutput.errors;

    if ("created_at" in blockOutput) {
      const _createdAt = blockOutput.created_at;

      if (typeof _createdAt === "string") {
        // ensure UTC parsing by appending 'Z' if not present
        const utcString = _createdAt.endsWith("Z")
          ? _createdAt
          : _createdAt + "Z";
        createdAt = new Date(utcString);
      }
    }
  }

  const codeOutput =
    blockOutput === null ? null : JSON.stringify(blockOutput, null, 2);

  const ago = createdAt ? formatMs(Date.now() - createdAt.getTime()).ago : null;

  const override = blockOutputStore.getOverride({
    wpid: workflowPermanentId,
    blockLabel,
  });

  const codeOverride = override ? JSON.stringify(override, null, 2) : null;

  return (
    <div className="flex h-full w-full flex-col">
      <header className="flex items-center justify-between">
        <SwitchBar
          className="mb-2 border-none"
          onChange={(value) => setPageName(value as PageName)}
          value={pageName}
          options={[
            {
              label: "Output",
              value: "output",
              helpText:
                "The last output from this block, when it completed successfully.",
            },
            {
              label: "Override",
              value: "override",
              helpText: "Supply your own override output.",
            },
          ]}
        />
        {pageName === "output" && (
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <header className="w-full text-right text-xs">{ago}</header>
              </TooltipTrigger>
              <TooltipContent>When the output was created</TooltipContent>
            </Tooltip>
          </TooltipProvider>
        )}
        {pageName === "override" && (
          <header className="flex w-full items-center justify-end gap-2 text-xs">
            <Label className="text-xs font-normal text-slate-300">
              Use Override
            </Label>
            <HelpTooltip content="Use this override instead of the last block output" />
            <Switch
              checked={useOverride}
              onCheckedChange={(value) => {
                blockOutputStore.setUseOverride({
                  wpid: workflowPermanentId,
                  blockLabel,
                  value,
                });
              }}
            />
          </header>
        )}
      </header>
      {pageName === "output" ? (
        <div className="flex h-full flex-1 flex-col gap-1 overflow-y-hidden border-2 border-transparent">
          {codeOutput ? (
            <>
              <CodeEditor
                key="output"
                className="nopan h-full w-full flex-1 overflow-y-scroll"
                language="json"
                value={codeOutput}
                lineWrap={false}
                readOnly
                fontSize={10}
                fullHeight
              />
            </>
          ) : (
            <div className="flex h-full w-full flex-1 items-center justify-center bg-slate-950">
              No output defined
            </div>
          )}
        </div>
      ) : (
        <div
          className={cn(
            "flex h-full flex-1 flex-col overflow-y-hidden border-2 border-transparent",
            {
              "border-[red]": overrideHasError,
            },
          )}
        >
          <CodeEditor
            key="override"
            className="nopan h-full w-full flex-1 overflow-y-scroll"
            language="json"
            value={codeOverride ?? ""}
            lineWrap={false}
            fontSize={10}
            fullHeight
            onChange={(value) => {
              try {
                JSON.parse(value), setOverrideHasError(false);
              } catch {
                setOverrideHasError(true);
                return;
              }
              const wasStored = blockOutputStore.setOverride({
                wpid: workflowPermanentId,
                blockLabel,
                data: JSON.parse(value),
              });

              if (!wasStored) {
                setOverrideHasError(true);
              }
            }}
          />
        </div>
      )}
    </div>
  );
}

export { BlockOutputs };
