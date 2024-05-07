import { getClient } from "@/api/AxiosClient";
import { Status, TaskApiResponse } from "@/api/types";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { StatusBadge } from "@/components/StatusBadge";
import { basicTimeFormat } from "@/util/timeFormat";
import { StepArtifactsLayout } from "./StepArtifactsLayout";
import { getRecordingURL, getScreenshotURL } from "./artifactUtils";
import { Skeleton } from "@/components/ui/skeleton";
import { Input } from "@/components/ui/input";
import { ZoomableImage } from "@/components/ZoomableImage";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Separator } from "@/components/ui/separator";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";

function TaskDetails() {
  const { taskId } = useParams();
  const credentialGetter = useCredentialGetter();

  const {
    data: task,
    isFetching: isTaskFetching,
    isError: isTaskError,
    error: taskError,
  } = useQuery<TaskApiResponse>({
    queryKey: ["task", taskId, "details"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client.get(`/tasks/${taskId}`).then((response) => response.data);
    },
    refetchInterval: (query) => {
      if (
        query.state.data?.status === Status.Running ||
        query.state.data?.status === Status.Queued
      ) {
        return 30000;
      }
      return false;
    },
    placeholderData: keepPreviousData,
  });

  if (isTaskError) {
    return <div>Error: {taskError?.message}</div>;
  }

  return (
    <div className="flex flex-col gap-8 max-w-6xl mx-auto p-8 pt-0">
      <div className="flex items-center">
        <Label className="w-32 shrink-0 text-lg">Task ID</Label>
        <Input value={taskId} readOnly />
      </div>
      <div className="flex items-center">
        <Label className="w-32 text-lg">Status</Label>
        {isTaskFetching ? (
          <Skeleton className="w-32 h-8" />
        ) : task ? (
          <StatusBadge status={task?.status} />
        ) : null}
      </div>
      {task?.status === Status.Completed ? (
        <div className="flex items-center">
          <Label className="w-32 shrink-0 text-lg">Extracted Information</Label>
          <Textarea
            rows={5}
            value={JSON.stringify(task.extracted_information, null, 2)}
            readOnly
          />
        </div>
      ) : null}
      {task?.status === Status.Failed || task?.status === Status.Terminated ? (
        <div className="flex items-center">
          <Label className="w-32 shrink-0 text-lg">Failure Reason</Label>
          <Textarea
            rows={5}
            value={JSON.stringify(task.failure_reason)}
            readOnly
          />
        </div>
      ) : null}
      {task ? (
        <Card>
          <CardHeader className="border-b-2">
            <CardTitle className="text-xl">Task Artifacts</CardTitle>
            <CardDescription>
              Recording and final screenshot of the task
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Tabs defaultValue="recording">
              <TabsList>
                <TabsTrigger value="recording">Recording</TabsTrigger>
                <TabsTrigger value="final-screenshot">
                  Final Screenshot
                </TabsTrigger>
              </TabsList>
              <TabsContent value="recording">
                {task.recording_url ? (
                  <video
                    width={800}
                    height={450}
                    src={getRecordingURL(task)}
                    controls
                  />
                ) : (
                  <div>No recording available</div>
                )}
              </TabsContent>
              <TabsContent value="final-screenshot">
                {task ? (
                  <div className="h-[450px] w-[800px]">
                    {task.screenshot_url ? (
                      <ZoomableImage
                        src={getScreenshotURL(task)}
                        alt="screenshot"
                        className="object-cover w-full h-full"
                      />
                    ) : (
                      <p>No screenshot available</p>
                    )}
                  </div>
                ) : null}
              </TabsContent>
            </Tabs>
          </CardContent>
        </Card>
      ) : null}
      <Card>
        <CardHeader className="border-b-2">
          <CardTitle className="text-lg">Steps</CardTitle>
          <CardDescription>Task Steps and Step Artifacts</CardDescription>
        </CardHeader>
        <CardContent className="min-h-96">
          <StepArtifactsLayout />
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="border-b-2">
          <CardTitle className="text-xl">Parameters</CardTitle>
          <CardDescription>Task URL and Input Parameters</CardDescription>
        </CardHeader>
        <CardContent className="py-8">
          {task ? (
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
                  value={task.request.navigation_goal}
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
                  value={task.request.data_extraction_goal}
                  readOnly
                />
              </div>
            </div>
          ) : null}
        </CardContent>
      </Card>
    </div>
  );
}

export { TaskDetails };
