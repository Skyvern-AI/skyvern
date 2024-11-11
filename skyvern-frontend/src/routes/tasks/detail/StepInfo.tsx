import { StepApiResponse } from "@/api/types";
import { StatusBadge } from "@/components/StatusBadge";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { basicLocalTimeFormat, basicTimeFormat } from "@/util/timeFormat";

type Props = {
  isFetching: boolean;
  stepProps?: StepApiResponse;
};

function StepInfo({ isFetching, stepProps }: Props) {
  return (
    <div className="flex flex-col gap-4 p-4">
      <div className="flex items-center">
        <Label className="w-24">Step ID:</Label>
        {isFetching ? (
          <Skeleton className="h-4 w-40" />
        ) : (
          <span>{stepProps?.step_id}</span>
        )}
      </div>
      <div className="flex items-center">
        <Label className="w-24">Status:</Label>
        {isFetching ? (
          <Skeleton className="h-4 w-40" />
        ) : stepProps ? (
          <StatusBadge status={stepProps.status} />
        ) : null}
      </div>
      <div className="flex items-center">
        <Label className="w-24">Created At:</Label>
        {isFetching ? (
          <Skeleton className="h-4 w-40" />
        ) : stepProps ? (
          <span title={basicTimeFormat(stepProps.created_at)}>
            {basicLocalTimeFormat(stepProps.created_at)}
          </span>
        ) : null}
      </div>
    </div>
  );
}

export { StepInfo };
