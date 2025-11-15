import { StepNavigation } from "./StepNavigation";
import { StepArtifacts } from "./StepArtifacts";
import { useQuery } from "@tanstack/react-query";
import { StepApiResponse } from "@/api/types";
import { useSearchParams } from "react-router-dom";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { apiPathPrefix } from "@/util/env";
import { useFirstParam } from "@/hooks/useFirstParam";

function StepArtifactsLayout() {
  const [searchParams, setSearchParams] = useSearchParams();
  const step = Number(searchParams.get("step")) || 0;
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

  const activeStep = steps?.[step];

  return (
    <div className="flex">
      <aside className="w-64 shrink-0">
        <StepNavigation
          activeIndex={step}
          onActiveIndexChange={(index) => {
            setSearchParams(
              (params) => {
                const newParams = new URLSearchParams(params);
                newParams.set("step", String(index));
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
