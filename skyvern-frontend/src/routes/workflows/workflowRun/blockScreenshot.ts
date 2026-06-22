import { ArtifactApiResponse, ArtifactType } from "@/api/types";
import { WorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";

// Agent blocks emit an LLM screenshot; code blocks emit per-action screenshots. Either one is a
// valid block-level screenshot for the run Overview.
function isBlockScreenshot(artifact: ArtifactApiResponse): boolean {
  return (
    artifact.artifact_type === ArtifactType.LLMScreenshot ||
    artifact.artifact_type === ArtifactType.ActionScreenshot
  );
}

// Artifacts arrive newest-first. An agent block is best represented by its LLM screenshot, but a
// code block's only LLM screenshot is the pre-execution one `Block.execute_safe` takes before the
// block runs; its real output is the per-action screenshot captured during execution, so prefer
// that and fall back to the LLM screenshot only when no action screenshot exists.
function selectBlockScreenshot(
  artifacts: Array<ArtifactApiResponse> | undefined,
  blockType?: string,
): ArtifactApiResponse | undefined {
  const findType = (type: ArtifactType) =>
    artifacts?.find((artifact) => artifact.artifact_type === type);
  const [primary, fallback] =
    blockType === WorkflowBlockTypes.Code
      ? [ArtifactType.ActionScreenshot, ArtifactType.LLMScreenshot]
      : [ArtifactType.LLMScreenshot, ArtifactType.ActionScreenshot];
  return findType(primary) ?? findType(fallback);
}

export { isBlockScreenshot, selectBlockScreenshot };
