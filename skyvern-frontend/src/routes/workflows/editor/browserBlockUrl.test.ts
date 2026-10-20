import type { Edge } from "@xyflow/react";
import { describe, expect, test } from "vitest";

import { RunEngine } from "@/api/types";

import { getBrowserBlockUrlError } from "./browserBlockUrl";
import type { AppNode } from "./nodes";
import {
  conditionalNodeDefaultData,
  createBranchCondition,
} from "./nodes/ConditionalNode/types";
import { loopNodeDefaultData } from "./nodes/LoopNode/types";
import {
  navigationNodeDefaultData,
  type NavigationNodeData,
} from "./nodes/NavigationNode/types";
import { urlNodeDefaultData } from "./nodes/URLNode/types";
import { isMissingRequiredStartUrl } from "./workflowEditorUtils";

const start = {
  id: "start",
  type: "start",
  position: { x: 0, y: 0 },
  data: {},
} as AppNode;

function navigation(
  id: string,
  overrides: Partial<NavigationNodeData> = {},
  parentId?: string,
): AppNode {
  return {
    id,
    type: "navigation",
    position: { x: 0, y: 0 },
    parentId,
    data: { ...navigationNodeDefaultData, label: id, ...overrides },
  } as AppNode;
}

function edge(source: string, target: string): Edge {
  return { id: `${source}-${target}`, source, target };
}

describe("isMissingRequiredStartUrl", () => {
  test("flags only an empty URL on the block that opens the first page", () => {
    const nodes = [start, navigation("first"), navigation("second")];
    const edges = [edge("start", "first"), edge("first", "second")];

    expect(isMissingRequiredStartUrl(nodes, edges, "first")).toBe(true);
    expect(isMissingRequiredStartUrl(nodes, edges, "second")).toBe(false);

    const withUrl = [
      start,
      navigation("first", { url: "https://example.com" }),
      navigation("second"),
    ];
    expect(isMissingRequiredStartUrl(withUrl, edges, "first")).toBe(false);
  });

  test("a 2.0 block derives its own URL and does not open a page for later blocks", () => {
    const nodes = [
      start,
      navigation("v2", { engine: RunEngine.SkyvernV2 }),
      navigation("v1"),
    ];
    const edges = [edge("start", "v2"), edge("v2", "v1")];

    expect(isMissingRequiredStartUrl(nodes, edges, "v2")).toBe(false);
    expect(isMissingRequiredStartUrl(nodes, edges, "v1")).toBe(true);
  });

  test("a block inside a loop is covered by a browser block before the loop", () => {
    const loop = {
      id: "loop",
      type: "loop",
      position: { x: 0, y: 0 },
      data: { ...loopNodeDefaultData, label: "loop" },
    } as AppNode;
    const nodes = [
      start,
      navigation("before"),
      loop,
      navigation("inner", {}, "loop"),
    ];
    const edges = [edge("start", "before"), edge("before", "loop")];

    expect(isMissingRequiredStartUrl(nodes, edges, "inner")).toBe(false);
  });
});

describe("isMissingRequiredStartUrl across block types and branches", () => {
  test("an empty first Go to URL block needs a URL", () => {
    const goto = {
      id: "goto",
      type: "url",
      position: { x: 0, y: 0 },
      data: { ...urlNodeDefaultData, label: "goto" },
    } as AppNode;

    expect(
      isMissingRequiredStartUrl([start, goto], [edge("start", "goto")], "goto"),
    ).toBe(true);
  });

  test("a conditional opens a page for later blocks only when every branch does", () => {
    const branch = createBranchCondition({ id: "branch" });
    const fallback = createBranchCondition({
      id: "fallback",
      is_default: true,
    });
    const conditional = {
      id: "cond",
      type: "conditional",
      position: { x: 0, y: 0 },
      data: {
        ...conditionalNodeDefaultData,
        label: "cond",
        branches: [branch, fallback],
      },
    } as AppNode;
    const inBranch = (id: string, branchId: string) =>
      ({
        ...navigation(id, { url: "https://example.com" }, "cond"),
        data: {
          ...navigationNodeDefaultData,
          label: id,
          url: "https://example.com",
          conditionalBranchId: branchId,
        },
      }) as AppNode;
    const edges = [edge("start", "cond"), edge("cond", "after")];

    const oneBranchOpensPage = [
      start,
      conditional,
      inBranch("a", "branch"),
      navigation("after"),
    ];
    expect(isMissingRequiredStartUrl(oneBranchOpensPage, edges, "after")).toBe(
      true,
    );

    const bothBranchesOpenPage = [
      ...oneBranchOpensPage,
      inBranch("b", "fallback"),
    ];
    expect(
      isMissingRequiredStartUrl(bothBranchesOpenPage, edges, "after"),
    ).toBe(false);
  });
});

describe("getBrowserBlockUrlError", () => {
  test.each([
    "https://example.com",
    "example.com/path?q=1",
    "http://localhost:3000/login",
    "http://internal-service:8080",
    "sub.example.co.uk",
    "192.168.1.10:8080",
    "münchen.de",
    "{{ starting_url }}",
    "https://{{ host }}/login",
    "",
  ])("accepts %j", (url) => {
    expect(getBrowserBlockUrlError(url)).toBeNull();
  });

  test.each([
    "example",
    "example.",
    "https://example.",
    "example..com",
    "example.c",
    "10",
    "ftp://example.com",
    "javascript:alert(1)",
    "https://",
  ])("rejects %j", (url) => {
    expect(getBrowserBlockUrlError(url)).not.toBeNull();
  });

  // The backend's urlparse reads "example.com:8080" as a scheme and rejects it,
  // so a scheme-less host:port must point the user at the fix, not look valid.
  test.each([
    ["example.com:8080", /https:\/\//],
    ["localhost:3000/login", /https:\/\//],
    ["ftp://example.com", /http:\/\/ and https:\/\//],
  ])("explains why %j is refused", (url, message) => {
    expect(getBrowserBlockUrlError(url)).toMatch(message);
  });
});
