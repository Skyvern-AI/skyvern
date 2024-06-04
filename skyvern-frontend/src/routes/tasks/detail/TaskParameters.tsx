import { getClient } from "@/api/AxiosClient";
import { TaskApiResponse } from "@/api/types";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { basicTimeFormat } from "@/util/timeFormat";
import { Label, Separator } from "@radix-ui/react-dropdown-menu";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";

function TaskParameters() {
  const { taskId } = useParams();
  const credentialGetter = useCredentialGetter();
  const {
    data: task,
    isFetching: taskIsFetching,
    isError: taskIsError,
  } = useQuery<TaskApiResponse>({
    queryKey: ["task", taskId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client.get(`/tasks/${taskId}`).then((response) => response.data);
    },
  });

  if (taskIsFetching) {
    return (
      <div className="h-[40rem]">
        <Skeleton className="h-full" />
      </div>
    );
  }

  if (taskIsError || !task) {
    return <div>Error loading parameters</div>;
  }

  return (
    <Card>
      <CardHeader className="border-b-2">
        <CardTitle className="text-xl">Parameters</CardTitle>
        <CardDescription>Task URL and Input Parameters</CardDescription>
      </CardHeader>
      <CardContent className="py-8">
        <div className="flex flex-col gap-8">
          <div className="flex items-center">
            <Label className="w-40 shrink-0">URL</Label>
            <Input value={task.request.url} readOnly />
          </div>
          <Separator />
          <div className="flex items-center">
            <Label className="w-40 shrink-0">Created at</Label>
            <Input value={basicTimeFormat(task.created_at)} readOnly />
          </div>
          <Separator />
          <div className="flex items-center">
            <Label className="w-40 shrink-0">Navigation Goal</Label>
            <Textarea
              rows={5}
              value={task.request.navigation_goal ?? ""}
              readOnly
            />
          </div>
          <Separator />
          <div className="flex items-center">
            <Label className="w-40 shrink-0">Navigation Payload</Label>
            <Textarea
              rows={5}
              value={
                typeof task.request.navigation_payload === "object"
                  ? JSON.stringify(task.request.navigation_payload, null, 2)
                  : task.request.navigation_payload
              }
              readOnly
            />
          </div>
          <Separator />
          <div className="flex items-center">
            <Label className="w-40 shrink-0">Data Extraction Goal</Label>
            <Textarea
              rows={5}
              value={task.request.data_extraction_goal ?? ""}
              readOnly
            />
          </div>
          <div className="flex items-center">
            <Label className="w-40 shrink-0">
              Extracted Information Schema
            </Label>
            <Textarea
              rows={5}
              value={
                typeof task.request.extracted_information_schema === "object"
                  ? JSON.stringify(
                      task.request.extracted_information_schema,
                      null,
                      2,
                    )
                  : task.request.extracted_information_schema
              }
              readOnly
            />
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

export { TaskParameters };
