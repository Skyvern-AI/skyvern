import { ActionsApiResponse, ActionTypes, Status } from "@/api/types";
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
};

function ActionCard({ action, onClick, active, index }: Props) {
  // Wait actions always succeed — they intentionally return ActionFailure
  // from the backend but completing a wait is expected, not a failure.
  const success =
    action.action_type === ActionTypes.wait ||
    action.status === Status.Completed ||
    action.status === Status.Skipped;

  return (
    <RunCard
      active={active}
      status={success ? "success" : "failure"}
      onClick={onClick}
      className="flex"
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
            {success ? (
              <StatusPill
                icon={<CheckCircledIcon className="h-4 w-4 text-success" />}
              />
            ) : (
              <StatusPill
                icon={<CrossCircledIcon className="h-4 w-4 text-destructive" />}
              />
            )}
          </div>
        </div>
        <div className="text-xs text-slate-400">{action.reasoning}</div>
        {action.action_type === ActionTypes.InputText && (
          <>
            <Separator />
            <div className="text-xs text-slate-400">
              Input:{" "}
              {action.action_type === "input_text"
                ? action.text ?? action.response
                : action.response}
            </div>
          </>
        )}
      </div>
    </RunCard>
  );
}

export { ActionCard };
