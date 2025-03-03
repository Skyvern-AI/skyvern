import { getClient } from "@/api/AxiosClient";
import { TaskV2 } from "@/api/types";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { ReloadIcon } from "@radix-ui/react-icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";
import { useNavigate } from "react-router-dom";

type Props = {
  exampleId: string;
  version: "v1" | "v2";
  icon: React.ReactNode;
  label: string;
  prompt: string;
};

function ExampleCasePill({ exampleId, version, icon, label, prompt }: Props) {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const startObserverCruiseMutation = useMutation({
    mutationFn: async (prompt: string) => {
      const client = await getClient(credentialGetter, "v2");
      return client.post<{ user_prompt: string }, { data: TaskV2 }>("/tasks", {
        user_prompt: prompt,
      });
    },
    onSuccess: (response) => {
      toast({
        variant: "success",
        title: "Workflow Run Created",
        description: `Workflow run created successfully.`,
      });

      queryClient.invalidateQueries({
        queryKey: ["workflowRuns"],
      });
      queryClient.invalidateQueries({
        queryKey: ["workflows"],
      });
      queryClient.invalidateQueries({
        queryKey: ["runs"],
      });

      navigate(
        `/workflows/${response.data.workflow_permanent_id}/${response.data.workflow_run_id}`,
      );
    },
    onError: (error: AxiosError) => {
      toast({
        variant: "destructive",
        title: "Error creating workflow run from prompt",
        description: error.message,
      });
    },
  });

  return (
    <div
      className="flex cursor-pointer gap-2 whitespace-normal rounded-sm bg-slate-elevation3 px-4 py-3 hover:bg-slate-elevation5 lg:whitespace-nowrap"
      onClick={() => {
        if (version === "v2") {
          startObserverCruiseMutation.mutate(prompt);
        } else {
          navigate(`/tasks/create/${exampleId}`);
        }
      }}
    >
      <div>
        {startObserverCruiseMutation.isPending ? (
          <ReloadIcon className="size-6 animate-spin" />
        ) : (
          icon
        )}
      </div>
      <div>{label}</div>
    </div>
  );
}

export { ExampleCasePill };
