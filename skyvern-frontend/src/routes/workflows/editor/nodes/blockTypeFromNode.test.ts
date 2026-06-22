import { describe, expect, it } from "vitest";
import { blockTypeFromNode } from "./blockTypeFromNode";

describe("blockTypeFromNode", () => {
  it("returns null when type is missing", () => {
    expect(blockTypeFromNode({})).toBeNull();
  });

  it("maps googleSheetsRead to google_sheets_read", () => {
    expect(blockTypeFromNode({ type: "googleSheetsRead" })).toBe(
      "google_sheets_read",
    );
  });

  it("maps googleSheetsWrite to google_sheets_write", () => {
    expect(blockTypeFromNode({ type: "googleSheetsWrite" })).toBe(
      "google_sheets_write",
    );
  });

  it("maps pdfFill to pdf_fill", () => {
    expect(blockTypeFromNode({ type: "pdfFill" })).toBe("pdf_fill");
  });

  it("maps url to goto_url", () => {
    expect(blockTypeFromNode({ type: "url" })).toBe("goto_url");
  });

  it("maps codeBlock to code", () => {
    expect(blockTypeFromNode({ type: "codeBlock" })).toBe("code");
  });

  it("maps loop to for_loop by default", () => {
    expect(blockTypeFromNode({ type: "loop", data: {} })).toBe("for_loop");
  });

  it("maps loop with loopKind=while to while_loop", () => {
    expect(
      blockTypeFromNode({ type: "loop", data: { loopKind: "while" } }),
    ).toBe("while_loop");
  });

  it("maps loop with loopKind=for_each to for_loop", () => {
    expect(
      blockTypeFromNode({ type: "loop", data: { loopKind: "for_each" } }),
    ).toBe("for_loop");
  });

  it("returns null for unknown values to surface missing mappings", () => {
    expect(blockTypeFromNode({ type: "newBlockType" })).toBeNull();
  });
});
