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
  DoubleArrowDownIcon,
  DownloadIcon,
  DropdownMenuIcon,
  HandIcon,
  InputIcon,
  KeyboardIcon,
  LightningBoltIcon,
  TimerIcon,
  UploadIcon,
} from "@radix-ui/react-icons";

const actionIcons: Partial<Record<ActionType, React.ReactNode>> = {
  [ActionTypes.Click]: <CursorArrowIcon className="h-4 w-4" />,
  [ActionTypes.Hover]: <HandIcon className="h-4 w-4" />,
  [ActionTypes.InputText]: <InputIcon className="h-4 w-4" />,
  [ActionTypes.DownloadFile]: <DownloadIcon className="h-4 w-4" />,
  [ActionTypes.UploadFile]: <UploadIcon className="h-4 w-4" />,
  [ActionTypes.SelectOption]: <DropdownMenuIcon className="h-4 w-4" />,
  [ActionTypes.wait]: <TimerIcon className="h-4 w-4" />,
  [ActionTypes.Scroll]: <DoubleArrowDownIcon className="h-4 w-4" />,
  [ActionTypes.KeyPress]: <KeyboardIcon className="h-4 w-4" />,
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
  // wait actions return ActionFailure despite succeeding
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
  // script-generated input text lives in action.response, not action.text
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
          "group rounded-md bg-slate-elevation4 ring-1 ring-transparent transition-all duration-200",
          {
            "ring-1 ring-neutral-500/45 hover:ring-neutral-500/45 dark:ring-white/40 dark:hover:ring-white/40":
              active,
            "hover:ring-neutral-400/40 dark:hover:ring-white/25": !active,
          },
          cardClassName,
        )}
      >
        <div className="flex items-center">
          <button
            type="button"
            onClick={onSelect}
            className="flex min-h-[40px] flex-1 cursor-pointer items-center gap-2 rounded-md px-3 py-2 text-left outline-none focus-visible:ring-1 focus-visible:ring-neutral-500/45 dark:focus-visible:ring-white/40"
          >
            <span
              aria-hidden="true"
              className={cn("h-2 w-2 shrink-0 rounded-full", {
                "bg-success": success,
                "bg-destructive": !success,
              })}
            />
            <span className="shrink-0 text-xs tabular-nums text-neutral-500 dark:text-slate-500">
              #{index}
            </span>
            {icon && (
              <span
                className="shrink-0 text-neutral-600 dark:text-slate-300"
                aria-hidden="true"
              >
                {icon}
              </span>
            )}
            <span className="shrink-0 text-xs text-neutral-700 dark:text-slate-300">
              {label}
            </span>
            <span
              className={cn(
                "min-w-0 flex-1 truncate text-xs text-neutral-800 dark:text-slate-200",
                reasoningPreview.length === 0 && "invisible",
              )}
            >
              {reasoningPreview}
            </span>
          </button>
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
          <button
            type="button"
            aria-label={expanded ? "Collapse details" : "Expand details"}
            aria-expanded={expanded}
            onClick={onToggleExpanded}
            className="mr-2 shrink-0 rounded p-0.5 text-neutral-500 outline-none hover:bg-neutral-200 hover:text-neutral-900 focus-visible:ring-1 focus-visible:ring-neutral-500/45 dark:text-slate-400 dark:hover:bg-slate-elevation3 dark:hover:text-slate-200 dark:focus-visible:ring-white/40"
          >
            {expanded ? (
              <ChevronDownIcon className="h-4 w-4" />
            ) : (
              <ChevronRightIcon className="h-4 w-4" />
            )}
          </button>
        </div>
        <CollapsibleContent className="space-y-2 px-3 pb-3 pt-1 text-xs text-neutral-600 dark:text-slate-400">
          {action.reasoning && (
            <div className="rounded bg-neutral-200/80 p-2 dark:bg-slate-elevation5">
              <div className="mb-1 text-[10px] uppercase tracking-wide text-neutral-500 dark:text-slate-500">
                Reasoning
              </div>
              <div className="whitespace-pre-wrap break-words text-neutral-700 dark:text-slate-300">
                {action.reasoning}
              </div>
            </div>
          )}
          {inputValue != null && inputValue.length > 0 && (
            <div className="rounded bg-neutral-200/80 p-2 dark:bg-slate-elevation5">
              <div className="mb-1 text-[10px] uppercase tracking-wide text-neutral-500 dark:text-slate-500">
                Input
              </div>
              <div className="whitespace-pre-wrap break-words font-mono text-neutral-700 dark:text-slate-300">
                {inputValue}
              </div>
            </div>
          )}
          {confidencePct != null && (
            <div className="flex items-center gap-2">
              <span className="text-[10px] uppercase tracking-wide text-neutral-500 dark:text-slate-500">
                Confidence
              </span>
              <span className="tabular-nums text-neutral-700 dark:text-slate-300">
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
