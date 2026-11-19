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
import { fileDownloadNodeDefaultData } from "./nodes/FileDownloadNode/types";
import { urlNodeDefaultData } from "./nodes/URLNode/types";
import { waitNodeDefaultData } from "./nodes/WaitNode/types";
import {
  isMissingRequiredStartUrl,
  startUrlRequiredIds,
} from "./workflowEditorUtils";

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

  test("an SFTP source needs no start URL and does not provide browser state", () => {
    const sftpSource = {
      id: "sftp-source",
      type: "fileDownload",
      position: { x: 0, y: 0 },
      data: {
        ...fileDownloadNodeDefaultData,
        label: "sftp-source",
        sftpSourcePath: "/reports/report.csv",
      },
    } as AppNode;
    const nodes = [start, sftpSource, navigation("after-sftp")];
    const edges = [
      edge("start", "sftp-source"),
      edge("sftp-source", "after-sftp"),
    ];

    expect(isMissingRequiredStartUrl(nodes, edges, "sftp-source")).toBe(false);
    expect(isMissingRequiredStartUrl(nodes, edges, "after-sftp")).toBe(true);
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

test("startUrlRequiredIds holds exactly the ids isMissingRequiredStartUrl flags", () => {
  const loop = (id: string, parentId?: string) =>
    ({
      id,
      type: "loop",
      position: { x: 0, y: 0 },
      parentId,
      data: { ...loopNodeDefaultData, label: id },
    }) as AppNode;
  const sftp = {
    id: "sftp",
    type: "fileDownload",
    position: { x: 0, y: 0 },
    data: {
      ...fileDownloadNodeDefaultData,
      label: "sftp",
      sftpSourcePath: "/reports/report.csv",
    },
  } as AppNode;
  const graphs: Array<[AppNode[], Edge[]]> = [
    [
      // A page opened two steps back still counts.
      [
        start,
        navigation("opened", { url: "https://example.com" }),
        sftp,
        navigation("later"),
      ],
      [edge("start", "opened"), edge("opened", "sftp"), edge("sftp", "later")],
    ],
    [
      [
        start,
        sftp,
        navigation("v2", { engine: RunEngine.SkyvernV2 }),
        navigation("a"),
        navigation("b"),
      ],
      [
        edge("start", "sftp"),
        edge("sftp", "v2"),
        edge("v2", "a"),
        edge("a", "b"),
      ],
    ],
    [
      // A loop whose first child opens the page, then a step after the loop.
      [
        start,
        loop("outer"),
        navigation("in1", {}, "outer"),
        navigation("in2", {}, "outer"),
        navigation("after"),
      ],
      [edge("start", "outer"), edge("in1", "in2"), edge("outer", "after")],
    ],
    [
      // A loop nested in a loop, both before any page is open.
      [
        start,
        loop("l1"),
        loop("l2", "l1"),
        navigation("deep", {}, "l2"),
        navigation("next"),
      ],
      [edge("start", "l1"), edge("l1", "next")],
    ],
  ];
  for (const [nodes, edges] of graphs)
    expect([...startUrlRequiredIds(nodes, edges)].sort()).toEqual(
      nodes
        .filter((node) => isMissingRequiredStartUrl(nodes, edges, node.id))
        .map((node) => node.id)
        .sort(),
    );
});

// Counts the reads of each edge's ends, so a pass that walks the edges once per step fails on any machine.
function countedEdges(ids: string[]): { edges: Edge[]; reads: () => number } {
  let reads = 0;
  const edges = ["start", ...ids].slice(0, -1).map((source, i) => {
    const target = ids[i]!;
    return {
      id: `${source}-${target}`,
      get source() {
        reads += 1;
        return source;
      },
      get target() {
        reads += 1;
        return target;
      },
    } as Edge;
  });
  return { edges, reads: () => reads };
}

test.each([
  ["2,000 page-opening steps", 2000, 2000],
  // A walk that recurses once per step would overflow the stack here.
  ["20,000 steps that open no page, then one that does", 20000, 1],
])(
  "startUrlRequiredIds reads each edge a fixed number of times on a chain of %s",
  (_case, length, browserSteps) => {
    const ids = Array.from({ length }, (_, i) => `n${i}`);
    const firstBrowser = length - browserSteps;
    const nodes = [
      start,
      ...ids.map((id, i) =>
        i >= firstBrowser
          ? navigation(id)
          : ({
              id,
              type: "wait",
              position: { x: 0, y: 0 },
              data: { ...waitNodeDefaultData, label: id },
            } as AppNode),
      ),
    ];
    const { edges, reads } = countedEdges(ids);
    expect([...startUrlRequiredIds(nodes, edges)]).toEqual([ids[firstBrowser]]);
    expect(reads()).toBeLessThanOrEqual(4 * edges.length);
  },
);

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
