import { StepNavigation } from "./StepNavigation";
import { StepArtifacts } from "./StepArtifacts";
import { useQuery } from "@tanstack/react-query";
import { StepApiResponse } from "@/api/types";
import { useSearchParams } from "react-router-dom";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { apiPathPrefix } from "@/util/env";
import { useFirstParam } from "@/hooks/useFirstParam";

function getRequestedStepIndex(stepParam: string | null): number {
  const step = Number(stepParam);
  return Number.isInteger(step) && step >= 0 ? step : 0;
}

function getActiveStepIndex(
  steps: Array<StepApiResponse> | undefined,
  stepParam: string | null,
  stepIdParam: string | null,
): number {
  const requestedStepIndex = getRequestedStepIndex(stepParam);
  if (!steps) return requestedStepIndex;

  if (stepIdParam) {
    const stepIdIndex = steps.findIndex((step) => step.step_id === stepIdParam);
    if (stepIdIndex !== -1) return stepIdIndex;
  }

  return requestedStepIndex < steps.length ? requestedStepIndex : 0;
}

function StepArtifactsLayout() {
  const [searchParams, setSearchParams] = useSearchParams();
  const credentialGetter = useCredentialGetter();
  const taskId = useFirstParam("taskId", "runId");

  const {
    data: steps,
    isError,
    error,
  } = useQuery<Array<StepApiResponse>>({
    queryKey: ["task", taskId, "steps"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get(`${apiPathPrefix}/tasks/${taskId}/steps`)
        .then((response) => response.data);
    },
  });

  if (isError) {
    return <div>Error: {error?.message}</div>;
  }

  const activeStepIndex = getActiveStepIndex(
    steps,
    searchParams.get("step"),
    searchParams.get("step_id"),
  );
  const activeStep = steps?.[activeStepIndex];

  return (
    <div className="flex">
      <aside className="w-64 shrink-0">
        <StepNavigation
          activeIndex={activeStepIndex}
          onActiveIndexChange={(index) => {
            setSearchParams(
              (params) => {
                const newParams = new URLSearchParams(params);
                newParams.set("step", String(index));
                newParams.delete("step_id");
                return newParams;
              },
              {
                replace: true,
              },
            );
          }}
        />
      </aside>
      <main className="w-full px-4">
        {activeStep ? (
          <StepArtifacts id={activeStep.step_id} stepProps={activeStep} />
        ) : null}
      </main>
    </div>
  );
}

export { StepArtifactsLayout };
