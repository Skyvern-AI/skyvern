import { useQuery } from "@tanstack/react-query";
import { isAxiosError } from "axios";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import {
  getActiveOrgQueryKeyScope,
  getOrgScopedQueryKey,
  useActiveOrgId,
} from "@/store/ActiveOrgContext";
import type { AccountGroupOutcome } from "../emberTypes";

const POLL_MS = 5000;

type GroupStatus = "active" | "cancel_requested" | "finished";

interface GroupItem {
  key: string;
  position: number;
  workflow_run_id: string;
  run_status: string | null;
  outcome: AccountGroupOutcome;
}

interface Group {
  workflow_run_group_id: string;
  status: GroupStatus;
  items: GroupItem[];
}

const OUTCOME_LABEL: Record<AccountGroupOutcome, string> = {
  pending: "Waiting",
  in_progress: "Running",
  completed: "Completed",
  failed: "Failed before acting",
  unknown: "Unknown, may have acted",
  canceled: "Canceled",
};

const OUTCOME_ORDER: AccountGroupOutcome[] = [
  "pending",
  "in_progress",
  "completed",
  "failed",
  "unknown",
  "canceled",
];

const UNFINISHED_OUTCOMES: ReadonlySet<AccountGroupOutcome> = new Set([
  "pending",
  "in_progress",
]);

function plural(count: number, noun: string): string {
  return count === 1 ? `1 ${noun}` : `${count} ${noun}s`;
}

const OUTCOME_CLASS: Partial<Record<AccountGroupOutcome, string>> = {
  failed: "text-rose-700 dark:text-rose-300",
  unknown: "text-amber-700 dark:text-yellow-400",
};

const STATUS_LABEL: Record<GroupStatus, string> = {
  active: "running",
  cancel_requested: "stopping",
  finished: "finished",
};

function isClientError(error: unknown): boolean {
  const status = isAxiosError(error) ? error.response?.status : undefined;
  return status !== undefined && status >= 400 && status < 500;
}

function useGroup(groupId: string | null) {
  const credentialGetter = useCredentialGetter();
  const scope = getActiveOrgQueryKeyScope(useActiveOrgId());
  return useQuery<Group>({
    queryKey: getOrgScopedQueryKey(["workflowRunGroup", groupId], scope),
    enabled: groupId !== null,
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const response = await client.get<Group>(
        `/workflow_run_groups/${groupId}`,
      );
      return response.data;
    },
    retry: (failureCount, error) => !isClientError(error) && failureCount < 3,
    refetchInterval: (query) =>
      query.state.data?.status === "finished" ||
      isClientError(query.state.error)
        ? false
        : POLL_MS,
  });
}

export {
  OUTCOME_CLASS,
  OUTCOME_LABEL,
  OUTCOME_ORDER,
  STATUS_LABEL,
  UNFINISHED_OUTCOMES,
  plural,
  useGroup,
};
export type { Group, GroupItem, GroupStatus };
