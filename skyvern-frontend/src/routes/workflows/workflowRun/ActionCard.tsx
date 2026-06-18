import { ActionsApiResponse, ActionTypes } from "@/api/types";
import { getActionDisplayKind } from "@/routes/workflows/components/actionStatus";
import { TerminatedIcon, terminatedTone } from "@/components/terminatedVisual";
import { StatusPill } from "@/components/ui/status-pill";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { Separator } from "@/components/ui/separator";
import { ActionTypePill } from "@/routes/tasks/detail/ActionTypePill";
import {
  CheckCircledIcon,
  CrossCircledIcon,
  LightningBoltIcon,
} from "@radix-ui/react-icons";
import { RunCard } from "./RunCard";

type Props = {
  action: ActionsApiResponse;
  index: number;
  active: boolean;
  onClick: React.DOMAttributes<HTMLDivElement>["onClick"];
  cardClassName?: string;
};

function ActionCard({ action, onClick, active, index, cardClassName }: Props) {
  const kind = getActionDisplayKind(action);

  return (
    <RunCard
      active={active}
      status={kind}
      onClick={onClick}
      className={cardClassName ? `flex ${cardClassName}` : "flex"}
    >
      <div className="flex-1 space-y-2 p-4 pl-5">
        <div className="flex justify-between">
          <div className="flex items-center gap-2">
            <span>#{index}</span>
          </div>
          <div className="flex items-center gap-2">
            <ActionTypePill actionType={action.action_type} />
            {action.created_by === "script" && (
              <TooltipProvider>
                <Tooltip delayDuration={300}>
                  <TooltipTrigger asChild>
                    <StatusPill
                      icon={
                        <LightningBoltIcon className="h-4 w-4 text-[gold]" />
                      }
                    />
                  </TooltipTrigger>
                  <TooltipContent className="max-w-[250px]">
                    Code Execution
                  </TooltipContent>
                </Tooltip>
              </TooltipProvider>
            )}
            {kind === "success" ? (
              <StatusPill
                icon={<CheckCircledIcon className="h-4 w-4 text-success" />}
              />
            ) : kind === "terminated" ? (
              <StatusPill
                icon={
                  <TerminatedIcon className={`h-4 w-4 ${terminatedTone}`} />
                }
              />
            ) : (
              <StatusPill
                icon={<CrossCircledIcon className="h-4 w-4 text-destructive" />}
              />
            )}
          </div>
        </div>
        <div className="break-words text-xs text-neutral-600 dark:text-slate-400">
          {action.reasoning}
        </div>
        {action.action_type === ActionTypes.InputText && (
          <>
            <Separator />
            <div className="text-xs text-neutral-600 dark:text-slate-400">
              Input:{" "}
              {action.action_type === "input_text"
                ? (action.text ?? action.response)
                : action.response}
            </div>
          </>
        )}
      </div>
    </RunCard>
  );
}

export { ActionCard };
