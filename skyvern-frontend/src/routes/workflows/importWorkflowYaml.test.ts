import { strToU8, zipSync } from "fflate";
import { describe, expect, it } from "vitest";
import { parse as parseYAML, stringify as convertToYAML } from "yaml";

import {
  expandFileToWorkflowYamls,
  extractTitleFromYaml,
  stripLegacyEngineFromYaml,
  unzipArchive,
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

  it("converts JSON strings so the backend's YAML 1.1 parser keeps them strings", () => {
    const [yaml] = expandFileToWorkflowYamls(
      JSON.stringify({ title: "on", goal: "1_000" }),
    );
    expect(parseYAML(yaml!, { version: "1.1" })).toEqual({
      title: "on",
      goal: "1_000",
    });
  });

  it("splits a bundle into each document's own text, so every value reaches the backend as written", () => {
    const first =
      "# first\ntitle: A\nflag: Y\nn: n\ncount: 1e3\nwhen: 2024-01-01 10:00:00-05:00\n";
    const second = "---\ntitle: B # kept\nflag: 'on'\n";
    const expanded = expandFileToWorkflowYamls(first + second);
    expect(expanded).toEqual([first, second]);
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
      "Workflow A.yaml": strToU8(convertToYAML(workflowA)),
      "Workflow B.yaml": strToU8(convertToYAML(workflowB)),
    });

    const entries = unzipArchive(zipped);
    const byName = new Map(entries.map((entry) => [entry.name, entry.text]));

    expect(entries).toHaveLength(2);
    expect(parseYAML(byName.get("Workflow A.yaml")!)).toEqual(workflowA);
    expect(parseYAML(byName.get("Workflow B.yaml")!)).toEqual(workflowB);
  });

  it("round-trips zipped per-agent files back into individual workflows", () => {
    const zipped = zipSync({
      "Workflow A.yaml": strToU8(convertToYAML(workflowA)),
      "Workflow B.yaml": strToU8(convertToYAML(workflowB)),
    });

    const expanded = unzipArchive(zipped).flatMap((entry) =>
      expandFileToWorkflowYamls(entry.text),
    );

    expect(titlesOf(expanded)).toEqual(["Workflow A", "Workflow B"]);
  });

  it("ignores empty entries", () => {
    const zipped = zipSync({
      "empty.yaml": new Uint8Array(0),
      "Workflow A.yaml": strToU8(convertToYAML(workflowA)),
    });

    const entries = unzipArchive(zipped);

    expect(entries.map((entry) => entry.name)).toEqual(["Workflow A.yaml"]);
  });

  it("ignores non-workflow files, including macOS zip metadata junk", () => {
    const zipped = zipSync({
      "Workflow A.yaml": strToU8(convertToYAML(workflowA)),
      "__MACOSX/._Workflow A.yaml": strToU8("junk"),
      ".DS_Store": strToU8("junk"),
      "notes.txt": strToU8("not a workflow"),
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
      files[`workflow-${i}.yaml`] = strToU8(convertToYAML(workflowA));
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

describe("stripLegacyEngineFromYaml", () => {
  const workflow = {
    title: "W",
    workflow_definition: {
      blocks: [
        { block_type: "navigation", label: "a", engine: "skyvern-1.0" },
        { block_type: "navigation", label: "b", engine: "skyvern-2.0" },
        { block_type: "navigation", label: "c", engine: "skyvern-3.0" },
        { block_type: "navigation", label: "d" },
        {
          block_type: "for_loop",
          label: "e",
          loop_blocks: [
            { block_type: "navigation", label: "f", engine: "skyvern-1.0" },
            { block_type: "navigation", label: "g", engine: "skyvern-3.0" },
          ],
        },
      ],
    },
  };

  it("unsets skyvern-1.0 at any depth and keeps every other engine", () => {
    const out = parseYAML(stripLegacyEngineFromYaml(convertToYAML(workflow)))
      .workflow_definition.blocks;
    expect(out[0]).not.toHaveProperty("engine");
    expect(out[1].engine).toBe("skyvern-2.0");
    expect(out[2].engine).toBe("skyvern-3.0");
    expect(out[3]).not.toHaveProperty("engine");
    expect(out[4].loop_blocks[0]).not.toHaveProperty("engine");
    expect(out[4].loop_blocks[1].engine).toBe("skyvern-3.0");
  });

  it("returns a file with no legacy engine byte-for-byte", () => {
    const clean =
      "# kept\ntitle: t\nworkflow_definition:\n  blocks:\n    - block_type: navigation\n      label: a\n      engine: skyvern-3.0\n";
    expect(stripLegacyEngineFromYaml(clean)).toBe(clean);
  });

  const lines = [
    "# exported",
    "title: t",
    "workflow_definition:",
    "  parameters:",
    "    - key: flag",
    "      default_value: Y",
    "    - key: other",
    "      default_value: n",
    "  blocks:",
    "    - block_type: navigation",
    "      label: a",
    "      engine: skyvern-1.0  # legacy",
    "      navigation_goal: 1e3",
    "      complete_criterion: 2001-12-14 21:59:43.10 -05:00",
    "    - engine: skyvern-1.0",
    "      block_type: navigation",
    "      label: b",
    "    - block_type: for_loop",
    "      label: c",
    "      loop_blocks:",
    "        - block_type: navigation",
    "          label: d",
    "          engine: 'skyvern-1.0'",
    "        - block_type: navigation",
    "          label: e",
    "          engine: skyvern-3.0",
    "    - block_type: navigation",
    "      label: f",
    "      engine: null",
    "",
  ];

  const withoutLegacy = [...lines];
  withoutLegacy.splice(22, 1);
  withoutLegacy.splice(14, 2, "    - block_type: navigation");
  withoutLegacy.splice(11, 1);

  it("removes only the skyvern-1.0 lines, at any depth, and leaves every other byte", () => {
    expect(stripLegacyEngineFromYaml(lines.join("\n"))).toBe(
      withoutLegacy.join("\n"),
    );
  });

  it("returns a file with no skyvern-1.0 byte-for-byte, values and comments included", () => {
    const clean = withoutLegacy.join("\n");
    expect(clean).toContain("engine: null");
    expect(stripLegacyEngineFromYaml(clean)).toBe(clean);
  });

  it("leaves a flow-style block holding skyvern-1.0 unchanged rather than rewrite it", () => {
    const flow =
      "workflow_definition:\n  blocks:\n    - {label: a, engine: skyvern-1.0, goal: Y}\n";
    expect(stripLegacyEngineFromYaml(flow)).toBe(flow);
  });

  it("returns unparseable text unchanged", () => {
    const bad = "title: [unclosed\n  engine: skyvern-1.0";
    expect(stripLegacyEngineFromYaml(bad)).toBe(bad);
  });
});
