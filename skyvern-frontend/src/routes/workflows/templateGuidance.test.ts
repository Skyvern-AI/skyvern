import { describe, expect, it } from "vitest";
import {
  buildTemplateGuidanceMessage,
  withoutTemplateViaParam,
} from "./templateGuidance";

type Input = Parameters<typeof buildTemplateGuidanceMessage>[0];

const withParams = (parameters: unknown[], blocks: unknown[] = []) =>
  ({
    title: "Invoice Downloading",
    description: null,
    workflow_definition: { parameters, blocks },
  }) as unknown as Input;

describe("buildTemplateGuidanceMessage", () => {
  it("points at the inputs card only when there are workflow inputs", () => {
    const withInput = buildTemplateGuidanceMessage(
      withParams([{ parameter_type: "workflow", key: "website_url" }]),
    );
    expect(withInput).toContain("inputs below");
    const outputOnly = buildTemplateGuidanceMessage(
      withParams([{ parameter_type: "output", key: "extracted" }]),
    );
    expect(outputOnly).toContain("run it as is");
    const credentialOnly = buildTemplateGuidanceMessage(
      withParams([{ parameter_type: "credential", key: "login" }]),
    );
    expect(credentialOnly).toContain("inputs below");
    expect(credentialOnly).toContain("never share a password");
    expect(credentialOnly).not.toContain("tell me the values");
  });

  it("summarizes blocks as numbered steps, falling back to the block type", () => {
    const message = buildTemplateGuidanceMessage(
      withParams(
        [],
        [
          {
            block_type: "navigation",
            navigation_goal: "Open the invoices page. Then click Download.",
          },
          { block_type: "goto_url", url: "https://example.com/login" },
          { block_type: "wait" },
        ],
      ),
    );
    expect(message).toContain("1. Open the invoices page.");
    expect(message).toContain("2. Open https://example.com/login");
    expect(message).toMatch(/\n3\. \S/);
    expect(message).not.toContain("Then click");
  });

  it("says it can run as is when there are no inputs", () => {
    expect(buildTemplateGuidanceMessage(withParams([]))).toContain(
      "run it as is",
    );
  });
});

describe("withoutTemplateViaParam", () => {
  it("drops only via=template", () => {
    expect(withoutTemplateViaParam("?via=template&panes=copilot")).toBe(
      "?panes=copilot",
    );
    expect(withoutTemplateViaParam("?via=discover")).toBe("?via=discover");
  });
});
