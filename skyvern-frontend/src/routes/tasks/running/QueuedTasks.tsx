import { getClient } from "@/api/AxiosClient";
import { TaskApiResponse } from "@/api/types";
import { useQuery } from "@tanstack/react-query";
import { basicTimeFormat } from "@/util/timeFormat";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useNavigate } from "react-router-dom";
import { StatusBadge } from "@/components/StatusBadge";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";

function QueuedTasks() {
  const navigate = useNavigate();
  const credentialGetter = useCredentialGetter();

  const { data: tasks } = useQuery<Array<TaskApiResponse>>({
    queryKey: ["tasks", "queued"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get("/tasks", {
          params: {
            task_status: "queued",
          },
        })
        .then((response) => response.data);
    },
  });

  if (tasks?.length === 0) {
    return <div>No queued tasks</div>;
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="w-1/3">URL</TableHead>
          <TableHead className="w-1/3">Status</TableHead>
          <TableHead className="w-1/3">Created At</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {tasks?.length === 0 ? (
          <TableRow>
            <TableCell colSpan={3}>No queued tasks</TableCell>
          </TableRow>
        ) : (
          tasks?.map((task) => {
            return (
              <TableRow
                key={task.task_id}
                className="cursor-pointer w-4 hover:bg-muted/50"
                onClick={() => {
                  navigate(task.task_id);
                }}
              >
                <TableCell className="w-1/3">{task.request.url}</TableCell>
                <TableCell className="w-1/3">
                  <StatusBadge status={task.status} />
                </TableCell>
                <TableCell className="w-1/3">
                  {basicTimeFormat(task.created_at)}
                </TableCell>
              </TableRow>
            );
          })
        )}
      </TableBody>
    </Table>
  );
}

export { QueuedTasks };
