import { client } from "@/api/AxiosClient";
import {
  ArtifactApiResponse,
  ArtifactType,
  StepApiResponse,
} from "@/api/types";
import { StatusBadge } from "@/components/StatusBadge";
import { Label } from "@/components/ui/label";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ZoomableImage } from "@/components/ZoomableImage";
import { Skeleton } from "@/components/ui/skeleton";
import { JSONArtifact } from "./JSONArtifact";
import { TextArtifact } from "./TextArtifact";
import { getImageURL } from "./artifactUtils";
import { Input } from "@/components/ui/input";
import { basicTimeFormat } from "@/util/timeFormat";

type Props = {
  id: string;
  stepProps: StepApiResponse;
};

function StepArtifacts({ id, stepProps }: Props) {
  const { taskId } = useParams();
  const {
    data: artifacts,
    isFetching,
    isError,
    error,
  } = useQuery<Array<ArtifactApiResponse>>({
    queryKey: ["task", taskId, "steps", id, "artifacts"],
    queryFn: async () => {
      return client
        .get(`/tasks/${taskId}/steps/${id}/artifacts`)
        .then((response) => response.data);
    },
  });

  if (isError) {
    return <div>Error: {error?.message}</div>;
  }

  const llmScreenshots = artifacts?.filter(
    (artifact) => artifact.artifact_type === ArtifactType.LLMScreenshot,
  );

  const actionScreenshots = artifacts?.filter(
    (artifact) => artifact.artifact_type === ArtifactType.ActionScreenshot,
  );

  const visibleElementsTreeTrimmed = artifacts?.find(
    (artifact) =>
      artifact.artifact_type === ArtifactType.VisibleElementsTreeTrimmed,
  );

  const llmPrompt = artifacts?.find(
    (artifact) => artifact.artifact_type === ArtifactType.LLMPrompt,
  );

  const llmResponseParsed = artifacts?.find(
    (artifact) => artifact.artifact_type === ArtifactType.LLMResponseParsed,
  );

  const htmlRaw = artifacts?.find(
    (artifact) => artifact.artifact_type === ArtifactType.HTMLScrape,
  );

  return (
    <Tabs defaultValue="info" className="w-full">
      <TabsList className="grid w-full h-16 grid-cols-4">
        <TabsTrigger value="info">Info</TabsTrigger>
        <TabsTrigger value="screenshot_llm">Annotated Screenshots</TabsTrigger>
        <TabsTrigger value="screenshot_action">Action Screenshots</TabsTrigger>
        <TabsTrigger value="element_tree_trimmed">Element Tree</TabsTrigger>
        <TabsTrigger value="llm_prompt">Prompt</TabsTrigger>
        <TabsTrigger value="llm_response_parsed">Action List</TabsTrigger>
        <TabsTrigger value="html_raw">HTML (Raw)</TabsTrigger>
      </TabsList>
      <TabsContent value="info">
        <div className="flex flex-col gap-6 p-4">
          <div className="flex items-center">
            <Label className="w-32 shrink-0">Step ID</Label>
            {isFetching ? (
              <Skeleton className="h-4 w-40" />
            ) : (
              <Input value={stepProps?.step_id} readOnly />
            )}
          </div>
          <div className="flex items-center">
            <Label className="w-32 shrink-0">Status</Label>
            {isFetching ? (
              <Skeleton className="h-4 w-40" />
            ) : stepProps ? (
              <StatusBadge status={stepProps.status} />
            ) : null}
          </div>
          <div className="flex items-center">
            <Label className="w-32 shrink-0">Created At</Label>
            {isFetching ? (
              <Skeleton className="h-4 w-40" />
            ) : stepProps ? (
              <Input value={basicTimeFormat(stepProps.created_at)} readOnly />
            ) : null}
          </div>
        </div>
      </TabsContent>
      <TabsContent value="screenshot_llm">
        {llmScreenshots && llmScreenshots.length > 0 ? (
          <div className="grid grid-cols-3 gap-4 p-4">
            {llmScreenshots.map((artifact, index) => (
              <ZoomableImage
                key={index}
                src={getImageURL(artifact)}
                className="object-cover w-full h-full"
                alt="action-screenshot"
              />
            ))}
          </div>
        ) : isFetching ? (
          <div className="grid grid-cols-3 gap-4 p-4">
            <Skeleton className="w-full h-full" />
            <Skeleton className="w-full h-full" />
            <Skeleton className="w-full h-full" />
          </div>
        ) : (
          <div>No screenshots found</div>
        )}
      </TabsContent>
      <TabsContent value="screenshot_action">
        {actionScreenshots && actionScreenshots.length > 0 ? (
          <div className="grid grid-cols-3 gap-4 p-4">
            {actionScreenshots.map((artifact, index) => (
              <ZoomableImage
                key={index}
                src={getImageURL(artifact)}
                className="object-cover w-full h-full"
                alt="action-screenshot"
              />
            ))}
          </div>
        ) : isFetching ? (
          <div className="grid grid-cols-3 gap-4 p-4">
            <Skeleton className="w-full h-full" />
            <Skeleton className="w-full h-full" />
            <Skeleton className="w-full h-full" />
          </div>
        ) : (
          <div>No screenshots found</div>
        )}
      </TabsContent>
      <TabsContent value="element_tree_trimmed">
        {visibleElementsTreeTrimmed ? (
          <JSONArtifact artifact={visibleElementsTreeTrimmed} />
        ) : null}
      </TabsContent>
      <TabsContent value="llm_prompt">
        {llmPrompt ? <TextArtifact artifact={llmPrompt} /> : null}
      </TabsContent>
      <TabsContent value="llm_response_parsed">
        {llmResponseParsed ? (
          <JSONArtifact artifact={llmResponseParsed} />
        ) : null}
      </TabsContent>
      <TabsContent value="html_raw">
        {htmlRaw ? <TextArtifact artifact={htmlRaw} /> : null}
      </TabsContent>
    </Tabs>
  );
}

export { StepArtifacts };
