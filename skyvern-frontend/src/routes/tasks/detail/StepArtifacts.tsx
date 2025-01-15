import { getClient } from "@/api/AxiosClient";
import {
  ArtifactApiResponse,
  ArtifactType,
  StepApiResponse,
} from "@/api/types";
import { StatusBadge } from "@/components/StatusBadge";
import { Label } from "@/components/ui/label";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ZoomableImage } from "@/components/ZoomableImage";
import { Skeleton } from "@/components/ui/skeleton";
import { getImageURL } from "./artifactUtils";
import { Input } from "@/components/ui/input";
import { basicLocalTimeFormat, basicTimeFormat } from "@/util/timeFormat";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { Artifact } from "./Artifact";
import { apiPathPrefix } from "@/util/env";

const enable_log_artifacts =
  import.meta.env.VITE_ENABLE_LOG_ARTIFACTS === "true";

type Props = {
  id: string;
  stepProps: StepApiResponse;
};

function StepArtifacts({ id, stepProps }: Props) {
  const [searchParams, setSearchParams] = useSearchParams();
  const artifact = searchParams.get("artifact") ?? "info";
  const credentialGetter = useCredentialGetter();
  const {
    data: artifacts,
    isFetching,
    isError,
    error,
  } = useQuery<Array<ArtifactApiResponse>>({
    queryKey: ["step", id, "artifacts"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get(`${apiPathPrefix}/step/${id}/artifacts`)
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

  const visibleElementsTree = artifacts?.filter(
    (artifact) => artifact.artifact_type === ArtifactType.VisibleElementsTree,
  );

  const llmRequest = artifacts?.filter(
    (artifact) => artifact.artifact_type === ArtifactType.LLMRequest,
  );

  const visibleElementsTreeInPrompt = artifacts?.filter(
    (artifact) =>
      artifact.artifact_type === ArtifactType.VisibleElementsTreeInPrompt,
  );

  const llmPrompt = artifacts?.filter(
    (artifact) => artifact.artifact_type === ArtifactType.LLMPrompt,
  );

  const llmResponseParsed = artifacts?.filter(
    (artifact) => artifact.artifact_type === ArtifactType.LLMResponseParsed,
  );

  const htmlRaw = artifacts?.filter(
    (artifact) => artifact.artifact_type === ArtifactType.HTMLScrape,
  );

  const skyvernLog = artifacts?.filter(
    (artifact) => artifact.artifact_type === ArtifactType.SkyvernLog,
  );

  return (
    <Tabs
      value={artifact}
      onValueChange={(value) => {
        setSearchParams(
          (params) => {
            const newParams = new URLSearchParams(params);
            newParams.set("artifact", value);
            return newParams;
          },
          {
            replace: true,
          },
        );
      }}
      className="w-full"
    >
      <TabsList className="grid h-16 w-full grid-cols-5">
        <TabsTrigger value="info">Info</TabsTrigger>
        <TabsTrigger value="screenshot_llm">Annotated Screenshots</TabsTrigger>
        <TabsTrigger value="screenshot_action">Action Screenshots</TabsTrigger>
        <TabsTrigger value="element_tree_trimmed">
          HTML Element Tree
        </TabsTrigger>
        <TabsTrigger value="element_tree">Element Tree</TabsTrigger>
        <TabsTrigger value="llm_prompt">Prompt</TabsTrigger>
        <TabsTrigger value="llm_response_parsed">Action List</TabsTrigger>
        <TabsTrigger value="html_raw">HTML (Raw)</TabsTrigger>
        <TabsTrigger value="llm_request">LLM Request (Raw)</TabsTrigger>
        {enable_log_artifacts && (
          <TabsTrigger value="skyvern_log">Skyvern Log</TabsTrigger>
        )}
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
              <Input
                value={basicLocalTimeFormat(stepProps.created_at)}
                readOnly
                title={basicTimeFormat(stepProps.created_at)}
              />
            ) : null}
          </div>
        </div>
      </TabsContent>
      <TabsContent value="screenshot_llm">
        {llmScreenshots && llmScreenshots.length > 0 ? (
          <div className="grid grid-cols-2 gap-4 p-4">
            {llmScreenshots.map((artifact, index) => (
              <ZoomableImage
                key={index}
                src={getImageURL(artifact)}
                className="h-full w-full object-cover"
                alt="action-screenshot"
              />
            ))}
          </div>
        ) : isFetching ? (
          <div className="grid grid-cols-2 gap-4 p-4">
            <Skeleton className="h-full w-full" />
            <Skeleton className="h-full w-full" />
            <Skeleton className="h-full w-full" />
          </div>
        ) : (
          <div>No screenshots found</div>
        )}
      </TabsContent>
      <TabsContent value="screenshot_action">
        {actionScreenshots && actionScreenshots.length > 0 ? (
          <div className="grid grid-cols-2 gap-4 p-4">
            {actionScreenshots.map((artifact, index) => (
              <ZoomableImage
                key={index}
                src={getImageURL(artifact)}
                className="h-full w-full object-cover"
                alt="action-screenshot"
              />
            ))}
          </div>
        ) : isFetching ? (
          <div className="grid grid-cols-3 gap-4 p-4">
            <Skeleton className="h-full w-full" />
            <Skeleton className="h-full w-full" />
            <Skeleton className="h-full w-full" />
          </div>
        ) : (
          <div>No screenshots found</div>
        )}
      </TabsContent>
      <TabsContent value="element_tree_trimmed">
        {visibleElementsTreeInPrompt ? (
          <Artifact type="html" artifacts={visibleElementsTreeInPrompt} />
        ) : null}
      </TabsContent>
      <TabsContent value="element_tree">
        {visibleElementsTree ? (
          <Artifact type="json" artifacts={visibleElementsTree} />
        ) : null}
      </TabsContent>
      <TabsContent value="llm_prompt">
        {llmPrompt ? <Artifact type="text" artifacts={llmPrompt} /> : null}
      </TabsContent>
      <TabsContent value="llm_response_parsed">
        {llmResponseParsed ? (
          <Artifact type="json" artifacts={llmResponseParsed} />
        ) : null}
      </TabsContent>
      <TabsContent value="html_raw">
        {htmlRaw ? <Artifact type="html" artifacts={htmlRaw} /> : null}
      </TabsContent>
      <TabsContent value="llm_request">
        {llmRequest ? <Artifact type="json" artifacts={llmRequest} /> : null}
      </TabsContent>
      {enable_log_artifacts && (
        <TabsContent value="skyvern_log">
          {skyvernLog ? <Artifact type="text" artifacts={skyvernLog} /> : null}
        </TabsContent>
      )}
    </Tabs>
  );
}

export { StepArtifacts };
