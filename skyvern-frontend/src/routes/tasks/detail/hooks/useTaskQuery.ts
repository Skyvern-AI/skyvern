import { getClient } from "@/api/AxiosClient";
import { Status, TaskApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { keepPreviousData, useQuery } from "@tanstack/react-query";

type Props = {
  id?: string;
};

function useTaskQuery({ id }: Props) {
  const credentialGetter = useCredentialGetter();

  return useQuery<TaskApiResponse>({
    queryKey: ["task", id],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client.get(`/tasks/${id}`).then((response) => response.data);
    },
    enabled: !!id,
    refetchInterval: (query) => {
      if (
        query.state.data?.status === Status.Running ||
        query.state.data?.status === Status.Queued
      ) {
        return 5000;
      }
      return false;
    },
    placeholderData: keepPreviousData,
  });
}

export { useTaskQuery };
