import { getClient } from "@/api/AxiosClient";
import { TaskV2 } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";

type Props = {
  id?: string;
};

function useTaskV2Query({ id }: Props) {
  const credentialGetter = useCredentialGetter();

  return useQuery<TaskV2>({
    queryKey: ["task_v2", id],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "v2");
      return client.get(`/tasks/${id}`).then((response) => response.data);
    },
    enabled: !!id,
  });
}

export { useTaskV2Query };
