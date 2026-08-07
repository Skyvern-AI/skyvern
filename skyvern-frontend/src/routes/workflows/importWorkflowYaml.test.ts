import { strToU8, zipSync } from "fflate";
import { describe, expect, it } from "vitest";
import { parse as parseYAML, stringify as convertToYAML } from "yaml";

import {
  expandFileToWorkflowYamls,
  extractTitleFromYaml,
  unzipArchive,
} from "./importWorkflowYaml";

const workflowA = { title: "Workflow A", workflow_definition: { blocks: [] } };
const workflowB = { title: "Workflow B", workflow_definition: { blocks: [] } };

function titlesOf(yamls: string[]): Array<string | null> {
  return yamls.map((yaml) => extractTitleFromYaml(yaml));
}

// Vitest's jsdom TextEncoder creates bytes in a different realm than fflate's
// Uint8Array constructor. Copy into a library-created array so zipSync treats
// the value as file data rather than recursively flattening its numeric keys.
function zipText(text: string): Uint8Array {
  const encoded = strToU8(text);
  const bytes = strToU8("\0".repeat(encoded.length), true);
  bytes.set(encoded);
  return bytes;
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

describe("unzipArchive", () => {
  it("extracts one text entry per file in the archive", () => {
    // Mirrors BulkActionBar.handleBulkExport's ZIP branch: one sanitized,
    // deduped entry per agent.
    const zipped = zipSync({
      "Workflow A.yaml": zipText(convertToYAML(workflowA)),
      "Workflow B.yaml": zipText(convertToYAML(workflowB)),
    });

    const entries = unzipArchive(zipped);
    const byName = new Map(entries.map((entry) => [entry.name, entry.text]));

    expect(entries).toHaveLength(2);
    expect(parseYAML(byName.get("Workflow A.yaml")!)).toEqual(workflowA);
    expect(parseYAML(byName.get("Workflow B.yaml")!)).toEqual(workflowB);
  });

  it("round-trips zipped per-agent files back into individual workflows", () => {
    const zipped = zipSync({
      "Workflow A.yaml": zipText(convertToYAML(workflowA)),
      "Workflow B.yaml": zipText(convertToYAML(workflowB)),
    });

    const expanded = unzipArchive(zipped).flatMap((entry) =>
      expandFileToWorkflowYamls(entry.text),
    );

    expect(titlesOf(expanded)).toEqual(["Workflow A", "Workflow B"]);
  });

  it("ignores empty entries", () => {
    const zipped = zipSync({
      "empty.yaml": zipText(""),
      "Workflow A.yaml": zipText(convertToYAML(workflowA)),
    });

    const entries = unzipArchive(zipped);

    expect(entries.map((entry) => entry.name)).toEqual(["Workflow A.yaml"]);
  });

  it("ignores non-workflow files, including macOS zip metadata junk", () => {
    const zipped = zipSync({
      "Workflow A.yaml": zipText(convertToYAML(workflowA)),
      "__MACOSX/._Workflow A.yaml": zipText("junk"),
      ".DS_Store": zipText("junk"),
      "notes.txt": zipText("not a workflow"),
    });

    const entries = unzipArchive(zipped);

    expect(entries.map((entry) => entry.name)).toEqual(["Workflow A.yaml"]);
  });

  it("rejects an archive over the size limit before attempting to unzip it", () => {
    const oversized = new Uint8Array(21 * 1024 * 1024);

    expect(() => unzipArchive(oversized)).toThrow(/too large/i);
  });

  it("rejects an archive with too many entries", () => {
    const files: Record<string, Uint8Array> = {};
    for (let i = 0; i < 201; i++) {
      files[`workflow-${i}.yaml`] = zipText(convertToYAML(workflowA));
    }
    const zipped = zipSync(files);

    expect(() => unzipArchive(zipped)).toThrow(/too many files/i);
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
