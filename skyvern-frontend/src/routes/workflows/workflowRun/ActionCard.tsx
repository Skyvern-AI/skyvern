import { ActionsApiResponse, ActionTypes, Status } from "@/api/types";
import { Separator } from "@/components/ui/separator";
import { ActionTypePill } from "@/routes/tasks/detail/ActionTypePill";
import { cn } from "@/util/utils";
import { CheckCircledIcon, CrossCircledIcon } from "@radix-ui/react-icons";
import { useCallback } from "react";

type Props = {
  action: ActionsApiResponse;
  index: number;
  active: boolean;
  onClick: React.DOMAttributes<HTMLDivElement>["onClick"];
};

function ActionCard({ action, onClick, active, index }: Props) {
  const success = action.status === Status.Completed;

  const refCallback = useCallback((element: HTMLDivElement | null) => {
    if (element && active) {
      element.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    }
    // this should only run once at mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div
      className={cn(
        "flex cursor-pointer rounded-lg border-2 border-transparent bg-slate-elevation3 hover:border-slate-50",
        {
          "border-l-destructive": !success,
          "border-l-success": success,
          "border-slate-50": active,
        },
      )}
      onClick={onClick}
      ref={refCallback}
    >
      <div className="flex-1 space-y-2 p-4 pl-5">
        <div className="flex justify-between">
          <div className="flex items-center gap-2">
            <span>#{index}</span>
          </div>
          <div className="flex items-center gap-2">
            <ActionTypePill actionType={action.action_type} />
            {success ? (
              <div className="flex gap-1 rounded-sm bg-slate-elevation5 px-2 py-1">
                <CheckCircledIcon className="h-4 w-4 text-success" />
                <span className="text-xs">Success</span>
              </div>
            ) : (
              <div className="flex gap-1 rounded-sm bg-slate-elevation5 px-2 py-1">
                <CrossCircledIcon className="h-4 w-4 text-destructive" />
                <span className="text-xs">Fail</span>
              </div>
            )}
          </div>
        </div>
        <div className="text-xs text-slate-400">{action.reasoning}</div>
        {action.action_type === ActionTypes.InputText && (
          <>
            <Separator />
            <div className="text-xs text-slate-400">
              Input: {action.response}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export { ActionCard };
