// @vitest-environment jsdom

import { describe, expect, test } from "vitest";

import { nodeTypes } from "./index";

describe("nodeTypes composition order", () => {
  test("every entry is a memoized exotic component (produced by wrapBlock helpers, not bare)", () => {
    for (const [type, component] of Object.entries(nodeTypes)) {
      expect(component, `nodeTypes.${type} should be set`).toBeDefined();
      expect(typeof component).toBe("object");
    }
  });

  test("nodeTypes covers the expected key set", () => {
    expect(new Set(Object.keys(nodeTypes))).toEqual(
      new Set([
        "loop",
        "conditional",
        "task",
        "textPrompt",
        "sendEmail",
        "codeBlock",
        "fileParser",
        "upload",
        "fileUpload",
        "download",
        "nodeAdder",
        "start",
        "validation",
        "action",
        "navigation",
        "human_interaction",
        "extraction",
        "login",
        "wait",
        "fileDownload",
        "pdfParser",
        "taskv2",
        "url",
        "http_request",
        "printPage",
        "workflowTrigger",
        "googleSheetsRead",
        "googleSheetsWrite",
        "pdfFill",
        "splitPdf",
      ]),
    );
  });
});
