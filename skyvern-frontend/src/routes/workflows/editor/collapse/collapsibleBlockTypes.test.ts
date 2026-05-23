import { describe, expect, test } from "vitest";

import {
  collapsibleRfNodeTypes,
  collapsibleWorkflowBlockTypes,
  toWorkflowBlockType,
} from "./collapsibleBlockTypes";

describe("collapsibleBlockTypes — loop registration", () => {
  test("loop RF node type is collapsible", () => {
    expect(collapsibleRfNodeTypes.has("loop")).toBe(true);
  });

  test("for_loop workflow block type is collapsible", () => {
    expect(collapsibleWorkflowBlockTypes.has("for_loop")).toBe(true);
  });

  test("while_loop workflow block type is collapsible", () => {
    expect(collapsibleWorkflowBlockTypes.has("while_loop")).toBe(true);
  });

  test("toWorkflowBlockType translates loop -> for_loop", () => {
    expect(toWorkflowBlockType("loop")).toBe("for_loop");
  });
});
