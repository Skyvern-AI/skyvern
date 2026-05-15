import {
  type ActionsApiResponse,
  type ActionType,
  ActionTypes,
  ReadableActionTypes,
  Status,
} from "@/api/types";
import { Collapsible, CollapsibleContent } from "@/components/ui/collapsible";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/util/utils";
import {
  ChevronDownIcon,
  ChevronRightIcon,
  CursorArrowIcon,
  DownloadIcon,
  HandIcon,
  InputIcon,
  LightningBoltIcon,
} from "@radix-ui/react-icons";

const actionIcons: Partial<Record<ActionType, React.ReactNode>> = {
  click: <CursorArrowIcon className="h-4 w-4" />,
  hover: <HandIcon className="h-4 w-4" />,
  input_text: <InputIcon className="h-4 w-4" />,
  download_file: <DownloadIcon className="h-4 w-4" />,
};

type Props = {
  action: ActionsApiResponse;
  index: number;
  active: boolean;
  expanded: boolean;
  onSelect: () => void;
  onToggleExpanded: () => void;
  cardClassName?: string;
};

function ActionCardCompact({
  action,
  index,
  active,
  expanded,
  onSelect,
  onToggleExpanded,
  cardClassName,
}: Props) {
  // Wait actions always succeed — they intentionally return ActionFailure
  // from the backend but completing a wait is expected, not a failure.
  const success =
    action.action_type === ActionTypes.wait ||
    action.status === Status.Completed ||
    action.status === Status.Skipped;

  const reasoningPreview = action.reasoning?.trim() ?? "";
  const fromScript = action.created_by === "script";
  const icon = actionIcons[action.action_type] ?? null;
  const label = ReadableActionTypes[action.action_type];
  const confidencePct =
    action.confidence_float != null
      ? Math.round(action.confidence_float * 100)
      : null;
  const inputValue =
    action.action_type === ActionTypes.InputText
      ? (action.text ?? action.response)
      : null;

  return (
    <Collapsible open={expanded} asChild>
      <div
        data-slot="action-card-compact"
        data-active={active ? "true" : "false"}
        data-status={success ? "success" : "failure"}
        className={cn(
          "group rounded-md border-l-2 bg-slate-elevation4 ring-1 ring-transparent transition-all duration-200",
          {
            "border-l-success": success && !active,
            "border-l-destructive": !success && !active,
            "border-l-transparent ring-2 ring-white/55 hover:ring-white/55":
              active,
            "hover:ring-white/25": !active,
          },
          cardClassName,
        )}
      >
        <div className="flex items-center">
          <button
            type="button"
            onClick={onSelect}
            className="flex min-h-[36px] flex-1 cursor-pointer items-center gap-2 rounded-md px-3 py-2 text-left outline-none focus-visible:ring-1 focus-visible:ring-white/40"
          >
            <span
              aria-hidden="true"
              className={cn("h-2 w-2 shrink-0 rounded-full", {
                "bg-success": success,
                "bg-destructive": !success,
              })}
            />
            <span className="shrink-0 text-xs tabular-nums text-slate-500">
              #{index}
            </span>
            {icon && (
              <span className="shrink-0 text-slate-300" aria-hidden="true">
                {icon}
              </span>
            )}
            <span className="shrink-0 text-xs font-semibold text-slate-200">
              {label}
            </span>
            {reasoningPreview.length > 0 && (
              <span className="min-w-0 flex-1 truncate text-xs text-slate-400">
                {reasoningPreview}
              </span>
            )}
            {reasoningPreview.length === 0 && <span className="flex-1" />}
            {fromScript && (
              <TooltipProvider>
                <Tooltip delayDuration={300}>
                  <TooltipTrigger asChild>
                    <span className="shrink-0" aria-label="Code Execution">
                      <LightningBoltIcon className="h-4 w-4 text-[gold]" />
                    </span>
                  </TooltipTrigger>
                  <TooltipContent className="max-w-[250px]">
                    Code Execution
                  </TooltipContent>
                </Tooltip>
              </TooltipProvider>
            )}
          </button>
          <button
            type="button"
            aria-label={expanded ? "Collapse details" : "Expand details"}
            aria-expanded={expanded}
            onClick={onToggleExpanded}
            className="mr-2 shrink-0 rounded p-0.5 text-slate-400 outline-none hover:bg-slate-elevation3 hover:text-slate-200 focus-visible:ring-1 focus-visible:ring-white/40"
          >
            {expanded ? (
              <ChevronDownIcon className="h-4 w-4" />
            ) : (
              <ChevronRightIcon className="h-4 w-4" />
            )}
          </button>
        </div>
        <CollapsibleContent className="space-y-2 px-3 pb-3 pt-1 text-xs text-slate-400">
          {action.reasoning && (
            <div className="rounded bg-slate-elevation3 p-2">
              <div className="mb-1 text-[10px] uppercase tracking-wide text-slate-500">
                Reasoning
              </div>
              <div className="whitespace-pre-wrap break-words text-slate-300">
                {action.reasoning}
              </div>
            </div>
          )}
          {inputValue != null && inputValue.length > 0 && (
            <div className="rounded bg-slate-elevation3 p-2">
              <div className="mb-1 text-[10px] uppercase tracking-wide text-slate-500">
                Input
              </div>
              <div className="whitespace-pre-wrap break-words font-mono text-slate-300">
                {inputValue}
              </div>
            </div>
          )}
          {confidencePct != null && (
            <div className="flex items-center gap-2">
              <span className="text-[10px] uppercase tracking-wide text-slate-500">
                Confidence
              </span>
              <span className="tabular-nums text-slate-300">
                {confidencePct}%
              </span>
            </div>
          )}
        </CollapsibleContent>
      </div>
    </Collapsible>
  );
}

export { ActionCardCompact };
