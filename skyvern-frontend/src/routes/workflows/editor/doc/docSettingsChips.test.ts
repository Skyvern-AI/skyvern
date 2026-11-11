import type { Node } from "@xyflow/react";
import { describe, expect, test } from "vitest";

import { extractionNodeDefaultData } from "../nodes/ExtractionNode/types";
import { getDocSettingsChips, getDocSettingsRows } from "./docSettingsChips";

describe("docSettingsChips", () => {
  const node = {
    id: "n",
    position: { x: 0, y: 0 },
    type: "extraction",
    data: {
      ...extractionNodeDefaultData,
      engine: "skyvern-2.0",
      maxStepsOverride: 12,
      maxRetries: 0,
      continueOnFailure: true,
      model: { model_name: "model-x" },
    },
  } as Node;

  test("chips name only the settings that differ from the step type's defaults", () => {
    expect(getDocSettingsChips(node)).toEqual([
      { key: "engine", label: "Engine: Skyvern 2.0" },
      { key: "maxStepsOverride", label: "Max steps: 12" },
      { key: "continueOnFailure", label: "Continue on failure: On" },
      { key: "model", label: "Model: model-x" },
    ]);
  });

  test("rows list the settings the type's form edits, defaults included", () => {
    expect(
      getDocSettingsRows(node).map((r) => [r.key, r.value, r.custom]),
    ).toEqual([
      ["engine", "Skyvern 2.0", true],
      ["maxStepsOverride", "12", true],
      ["continueOnFailure", "On", true],
      ["model", "model-x", true],
      ["disableCache", "Off", false],
    ]);
  });
});
