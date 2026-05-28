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

  // Only the input value is worth hiding behind a chevron — it can be long
  // or sensitive. Confidence is short metadata so we render it inline.
  const hasExpandableDetail = inputValue != null && inputValue.length > 0;

  return (
    <Collapsible open={expanded} asChild>
      <div
        data-slot="action-card-compact"
        data-active={active ? "true" : "false"}
        data-status={success ? "success" : "failure"}
        className={cn(
          "group relative rounded-md bg-slate-elevation4 ring-1 ring-transparent transition-all duration-200",
          {
            "ring-1 ring-white/40 hover:ring-white/40": active,
            "hover:ring-white/25": !active,
          },
          cardClassName,
        )}
      >
        <button
          type="button"
          onClick={onSelect}
          className={cn(
            "flex w-full cursor-pointer flex-col gap-1 rounded-md px-3 py-2 text-left outline-none focus-visible:ring-1 focus-visible:ring-white/40",
            // Reserve room for the absolutely-positioned chevron so the
            // confidence chip doesn't end up under it.
            hasExpandableDetail && "pr-10",
          )}
        >
          <div className="flex min-h-[24px] items-center gap-2">
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
            <span className="shrink-0 text-xs text-slate-300">{label}</span>
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
            {confidencePct != null && (
              <span className="ml-auto shrink-0 rounded bg-slate-elevation5 px-1.5 py-0.5 text-[10px] tabular-nums text-slate-400">
                {confidencePct}%
              </span>
            )}
          </div>
          {reasoningPreview.length > 0 && (
            <div className="whitespace-pre-wrap break-words text-xs text-slate-200">
              {reasoningPreview}
            </div>
          )}
        </button>
        {hasExpandableDetail && (
          // Sibling button (not nested) so the outer select button doesn't
          // contain interactive content. Absolute-positioned to keep the
          // visual chevron in the top-right corner of the card.
          <button
            type="button"
            onClick={onToggleExpanded}
            aria-label={expanded ? "Collapse details" : "Expand details"}
            aria-expanded={expanded}
            className="absolute right-3 top-2.5 cursor-pointer rounded p-0.5 text-slate-400 outline-none hover:bg-slate-elevation3 hover:text-slate-200 focus-visible:ring-1 focus-visible:ring-white/40"
          >
            {expanded ? (
              <ChevronDownIcon className="h-4 w-4" />
            ) : (
              <ChevronRightIcon className="h-4 w-4" />
            )}
          </button>
        )}
        <CollapsibleContent className="space-y-2 px-3 pb-3 pt-1 text-xs text-slate-400">
          {inputValue != null && inputValue.length > 0 && (
            <div className="rounded bg-slate-elevation5 p-2">
              <div className="mb-1 text-[10px] uppercase tracking-wide text-slate-500">
                Input
              </div>
              <div className="whitespace-pre-wrap break-words font-mono text-slate-300">
                {inputValue}
              </div>
            </div>
          )}
        </CollapsibleContent>
      </div>
    </Collapsible>
  );
}

export { ActionCardCompact };
