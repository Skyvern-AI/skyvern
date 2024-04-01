import { client } from "@/api/AxiosClient";
import { Status, TaskApiResponse } from "@/api/types";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { StepList } from "./StepList";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { TaskStatusBadge } from "@/components/TaskStatusBadge";
import { artifactApiBaseUrl } from "@/util/env";
import { Button } from "@/components/ui/button";
import { ReloadIcon } from "@radix-ui/react-icons";
import { basicTimeFormat } from "@/util/timeFormat";

function TaskDetails() {
  const { taskId } = useParams();

  const {
    data: task,
    isFetching: isTaskFetching,
    isError: isTaskError,
    error: taskError,
    refetch,
  } = useQuery<TaskApiResponse>({
    queryKey: ["task", taskId],
    queryFn: async () => {
      return client.get(`/tasks/${taskId}`).then((response) => response.data);
    },
    placeholderData: keepPreviousData,
  });

  if (isTaskError) {
    return <div>Error: {taskError?.message}</div>;
  }

  if (isTaskFetching) {
    return <div>Loading...</div>; // TODO: skeleton
  }

  if (!task) {
    return <div>Task not found</div>;
  }

  return (
    <div>
      <div className="flex flex-col gap-4 relative">
        <Button
          variant="ghost"
          size="icon"
          className="cursor-pointer absolute top-0 right-0"
          onClick={() => {
            refetch();
          }}
        >
          <ReloadIcon />
        </Button>
        {task.recording_url ? (
          <div className="flex">
            <Label className="w-32">Recording</Label>
            <video
              src={`${artifactApiBaseUrl}/artifact?path=${task.recording_url.slice(7)}`}
              controls
            />
          </div>
        ) : null}
        <div className="flex items-center">
          <Label className="w-32">Status</Label>
          <TaskStatusBadge status={task.status} />
        </div>
        {task.status === Status.Completed ? (
          <div className="flex items-center">
            <Label className="w-32 shrink-0">Extracted Information</Label>
            <Textarea
              rows={5}
              value={JSON.stringify(task.extracted_information, null, 2)}
              readOnly
            />
          </div>
        ) : null}
        {task.status === Status.Failed || task.status === Status.Terminated ? (
          <div className="flex items-center">
            <Label className="w-32 shrink-0">Failure Reason</Label>
            <Textarea
              rows={5}
              value={JSON.stringify(task.failure_reason)}
              readOnly
            />
          </div>
        ) : null}
      </div>
      <Accordion type="single" collapsible>
        <AccordionItem value="task-details">
          <AccordionTrigger>
            <h1>Task Parameters</h1>
          </AccordionTrigger>
          <AccordionContent>
            <div>
              <p className="py-2">Task ID: {taskId}</p>
              <p className="py-2">URL: {task.request.url}</p>
              <p className="py-2">{basicTimeFormat(task.created_at)}</p>
              <div className="py-2">
                <Label>Navigation Goal</Label>
                <Textarea
                  rows={5}
                  value={task.request.navigation_goal}
                  readOnly
                />
              </div>
              <div className="py-2">
                <Label>Navigation Payload</Label>
                <Textarea
                  rows={5}
                  value={task.request.navigation_payload}
                  readOnly
                />
              </div>
              <div className="py-2">
                <Label>Data Extraction Goal</Label>
                <Textarea
                  rows={5}
                  value={task.request.data_extraction_goal}
                  readOnly
                />
              </div>
            </div>
          </AccordionContent>
        </AccordionItem>
      </Accordion>
      <div className="py-2">
        <h1>Task Steps</h1>
        <StepList />
      </div>
    </div>
  );
}

export { TaskDetails };
