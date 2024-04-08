import { client } from "@/api/AxiosClient";
import { Status, TaskApiResponse } from "@/api/types";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { StatusBadge } from "@/components/StatusBadge";
import { artifactApiBaseUrl } from "@/util/env";
import { Button } from "@/components/ui/button";
import { ReloadIcon } from "@radix-ui/react-icons";
import { basicTimeFormat } from "@/util/timeFormat";
import { StepArtifactsLayout } from "./StepArtifactsLayout";
import Zoom from "react-medium-image-zoom";
import { AspectRatio } from "@/components/ui/aspect-ratio";

function TaskDetails() {
  const { taskId } = useParams();

  const {
    data: task,
    isFetching: isTaskFetching,
    isError: isTaskError,
    error: taskError,
    refetch,
  } = useQuery<TaskApiResponse>({
    queryKey: ["task", taskId, "details"],
    queryFn: async () => {
      return client.get(`/tasks/${taskId}`).then((response) => response.data);
    },
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
              src={`${artifactApiBaseUrl}/artifact/recording?path=${task.recording_url.slice(7)}`}
              controls
            />
          </div>
        ) : null}
        <div className="flex items-center">
          <Label className="w-32">Status</Label>
          <StatusBadge status={task.status} />
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
      <Accordion type="multiple">
        <AccordionItem value="task-parameters">
          <AccordionTrigger>
            <h1>Task Parameters</h1>
          </AccordionTrigger>
          <AccordionContent>
            <div>
              <p className="py-2">Task ID: {taskId}</p>
              <p className="py-2">URL: {task.request.url}</p>
              <p className="py-2">
                Created: {basicTimeFormat(task.created_at)}
              </p>
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
        <AccordionItem value="task-artifacts">
          <AccordionTrigger>
            <h1>Screenshot</h1>
          </AccordionTrigger>
          <AccordionContent>
            <div className="max-w-sm mx-auto">
              {task.screenshot_url ? (
                <Zoom zoomMargin={16}>
                  <AspectRatio ratio={16 / 9}>
                    <img
                      src={`${artifactApiBaseUrl}/artifact/image?path=${task.screenshot_url.slice(7)}`}
                      alt="screenshot"
                    />
                  </AspectRatio>
                </Zoom>
              ) : (
                <p>No screenshot</p>
              )}
            </div>
          </AccordionContent>
        </AccordionItem>
        <AccordionItem value="task-steps">
          <AccordionTrigger>
            <h1>Task Steps</h1>
          </AccordionTrigger>
          <AccordionContent>
            <StepArtifactsLayout />
          </AccordionContent>
        </AccordionItem>
      </Accordion>
    </div>
  );
}

export { TaskDetails };
