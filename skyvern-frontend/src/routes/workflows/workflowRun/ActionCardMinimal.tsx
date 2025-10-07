import { LightningBoltIcon } from "@radix-ui/react-icons";
import { ActionsApiResponse, Status } from "@/api/types";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { ActionTypePillMinimal } from "@/routes/tasks/detail/ActionTypePillMinimal";
import { ItemStatusIndicator } from "./ItemStatusIndicator";

type Props = {
  action: ActionsApiResponse;
};

function ActionCardMinimal({ action }: Props) {
  const success =
    action.status === Status.Completed || action.status === Status.Skipped;

  return (
    <ItemStatusIndicator failure={!success} success={success} offset="-0.7rem">
      <div className="flex items-center justify-center gap-2">
        <ActionTypePillMinimal actionType={action.action_type} />
        {action.created_by === "script" && (
          <TooltipProvider>
            <Tooltip delayDuration={300}>
              <TooltipTrigger asChild>
                <div className="flex gap-1">
                  <LightningBoltIcon className="h-4 w-4 text-[gold]" />
                </div>
              </TooltipTrigger>
              <TooltipContent className="max-w-[250px]">
                Code Execution
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        )}
      </div>
    </ItemStatusIndicator>
  );
}

export { ActionCardMinimal };
