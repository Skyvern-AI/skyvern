import { describe, expect, test } from "vitest";
import { ArtifactApiResponse, ArtifactType } from "@/api/types";
import { WorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";
import { isBlockScreenshot, selectBlockScreenshot } from "./blockScreenshot";

function artifact(
  artifactType: ArtifactType,
  artifactId = "a_1",
): ArtifactApiResponse {
  return {
    created_at: "2026-06-16T00:00:00Z",
    modified_at: "2026-06-16T00:00:00Z",
    artifact_id: artifactId,
    task_id: "tsk_1",
    step_id: "stp_1",
    artifact_type: artifactType,
    uri: "s3://bucket/key",
    organization_id: "o_1",
  };
}

describe("isBlockScreenshot", () => {
  test("true for LLM and action screenshots", () => {
    expect(isBlockScreenshot(artifact(ArtifactType.LLMScreenshot))).toBe(true);
    expect(isBlockScreenshot(artifact(ArtifactType.ActionScreenshot))).toBe(
      true,
    );
  });

  test("false for non-screenshot artifacts", () => {
    expect(isBlockScreenshot(artifact(ArtifactType.Recording))).toBe(false);
  });
});

describe("selectBlockScreenshot", () => {
  test("agent block prefers the LLM screenshot when present", () => {
    const artifacts = [
      artifact(ArtifactType.ActionScreenshot, "a_action"),
      artifact(ArtifactType.LLMScreenshot, "a_llm"),
    ];
    expect(selectBlockScreenshot(artifacts)?.artifact_id).toBe("a_llm");
  });

  test("code block prefers the action screenshot over the pre-execution LLM screenshot", () => {
    // execute_safe captures a block-start LLM screenshot before the code runs; the action
    // screenshot is the block's real output and must win for code blocks.
    const artifacts = [
      artifact(ArtifactType.ActionScreenshot, "a_action"),
      artifact(ArtifactType.LLMScreenshot, "a_llm_preexec"),
    ];
    expect(
      selectBlockScreenshot(artifacts, WorkflowBlockTypes.Code)?.artifact_id,
    ).toBe("a_action");
  });

  test("code block falls back to the LLM screenshot when no action screenshot exists", () => {
    const artifacts = [artifact(ArtifactType.LLMScreenshot, "a_llm")];
    expect(
      selectBlockScreenshot(artifacts, WorkflowBlockTypes.Code)?.artifact_id,
    ).toBe("a_llm");
  });

  test("falls back to the latest action screenshot when no LLM screenshot exists", () => {
    // Artifacts arrive newest-first, so the first action screenshot is the latest.
    const artifacts = [
      artifact(ArtifactType.ActionScreenshot, "a_action_newest"),
      artifact(ArtifactType.ActionScreenshot, "a_action_older"),
    ];
    expect(selectBlockScreenshot(artifacts)?.artifact_id).toBe(
      "a_action_newest",
    );
  });

  test("returns undefined when no screenshot is present", () => {
    expect(
      selectBlockScreenshot([artifact(ArtifactType.Recording)]),
    ).toBeUndefined();
    expect(selectBlockScreenshot([])).toBeUndefined();
    expect(selectBlockScreenshot(undefined)).toBeUndefined();
  });
});
