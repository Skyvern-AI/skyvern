import { ListBulletIcon } from "@radix-ui/react-icons";

import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { ScrollArea, ScrollAreaViewport } from "@/components/ui/scroll-area";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { cn } from "@/util/utils";

export type RunInputMeta = { label: string; value: string };

type RunInputsButtonProps = {
  // Ordered [key, value] entries for the agent (workflow) inputs this run used.
  parameters: Array<[string, unknown]>;
  // Run-level non-parameter inputs (webhook, proxy, headers, …).
  meta: RunInputMeta[];
};

export function RunInputsButton({ parameters, meta }: RunInputsButtonProps) {
  if (parameters.length === 0 && meta.length === 0) {
    return null;
  }

  const agentInputs = Object.fromEntries(parameters);

  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className={cn(
            "inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium",
            "text-muted-foreground hover:bg-slate-elevation3 hover:text-foreground",
            "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
          )}
        >
          <ListBulletIcon className="h-4 w-4" />
          Inputs
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        className="flex w-[30rem] max-w-[90vw] flex-col gap-4"
      >
        {parameters.length > 0 ? (
          <div className="flex flex-col gap-2">
            <span className="text-xs font-semibold text-foreground">
              Agent inputs
            </span>
            <CodeEditor
              language="json"
              value={JSON.stringify(agentInputs, null, 2)}
              readOnly
              maxHeight="220px"
            />
          </div>
        ) : null}
        {meta.length > 0 ? (
          <div className="flex flex-col gap-2">
            <span className="text-xs font-semibold text-foreground">
              Other inputs
            </span>
            <ScrollArea>
              <ScrollAreaViewport className="max-h-[180px]">
                <div className="flex flex-col gap-2">
                  {meta.map((entry) => (
                    <div
                      key={entry.label}
                      className="flex flex-col gap-0.5 text-sm"
                    >
                      <span className="text-xs text-muted-foreground">
                        {entry.label}
                      </span>
                      <span className="break-all">{entry.value}</span>
                    </div>
                  ))}
                </div>
              </ScrollAreaViewport>
            </ScrollArea>
          </div>
        ) : null}
      </PopoverContent>
    </Popover>
  );
}
