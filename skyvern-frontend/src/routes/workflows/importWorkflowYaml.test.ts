import { describe, expect, it } from "vitest";
import { parse as parseYAML, stringify as convertToYAML } from "yaml";

import {
  expandFileToWorkflowYamls,
  extractTitleFromYaml,
} from "./importWorkflowYaml";

const workflowA = { title: "Workflow A", workflow_definition: { blocks: [] } };
const workflowB = { title: "Workflow B", workflow_definition: { blocks: [] } };

function titlesOf(yamls: string[]): Array<string | null> {
  return yamls.map((yaml) => extractTitleFromYaml(yaml));
}

describe("expandFileToWorkflowYamls", () => {
  it("splits the bulk YAML export format into one workflow per document", () => {
    // Matches BulkActionBar.handleBulkExport: docs joined by "---\n".
    const bundle = [workflowA, workflowB]
      .map((definition) => convertToYAML(definition))
      .join("---\n");

    const expanded = expandFileToWorkflowYamls(bundle);

    expect(expanded).toHaveLength(2);
    expect(titlesOf(expanded)).toEqual(["Workflow A", "Workflow B"]);
    expect(parseYAML(expanded[0]!)).toEqual(workflowA);
    expect(parseYAML(expanded[1]!)).toEqual(workflowB);
  });

  it("splits a top-level JSON array into one workflow per element", () => {
    const bundle = JSON.stringify([workflowA, workflowB], null, 2);

    const expanded = expandFileToWorkflowYamls(bundle);

    expect(expanded).toHaveLength(2);
    expect(titlesOf(expanded)).toEqual(["Workflow A", "Workflow B"]);
    expect(parseYAML(expanded[0]!)).toEqual(workflowA);
  });

  it("returns a single-workflow YAML file unchanged", () => {
    const single = convertToYAML(workflowA);

    const expanded = expandFileToWorkflowYamls(single);

    expect(expanded).toEqual([single]);
  });

  it("converts a single-workflow JSON object into one YAML", () => {
    const single = JSON.stringify(workflowA, null, 2);

    const expanded = expandFileToWorkflowYamls(single);

    expect(expanded).toHaveLength(1);
    expect(parseYAML(expanded[0]!)).toEqual(workflowA);
  });

  it("ignores empty documents from a trailing separator", () => {
    const bundle = `${convertToYAML(workflowA)}---\n${convertToYAML(
      workflowB,
    )}---\n`;

    const expanded = expandFileToWorkflowYamls(bundle);

    expect(expanded).toHaveLength(2);
    expect(titlesOf(expanded)).toEqual(["Workflow A", "Workflow B"]);
  });

  it("throws on a bundle with a malformed document instead of importing truncated data", () => {
    const bundle = `${convertToYAML(workflowA)}---\ntitle: Broken\nblocks: [1, 2\n`;

    expect(() => expandFileToWorkflowYamls(bundle)).toThrow();
  });

  it("returns a single JSON array element as one workflow", () => {
    const bundle = JSON.stringify([workflowA]);

    const expanded = expandFileToWorkflowYamls(bundle);

    expect(expanded).toHaveLength(1);
    expect(parseYAML(expanded[0]!)).toEqual(workflowA);
  });
});

describe("extractTitleFromYaml", () => {
  it("reads and trims a top-level title", () => {
    expect(extractTitleFromYaml("title: '  Padded  '\nfoo: 1")).toBe("Padded");
  });

  it("returns null when there is no usable title", () => {
    expect(extractTitleFromYaml("foo: 1")).toBeNull();
    expect(extractTitleFromYaml("title: ''")).toBeNull();
    expect(extractTitleFromYaml(": : invalid : :")).toBeNull();
  });
});
