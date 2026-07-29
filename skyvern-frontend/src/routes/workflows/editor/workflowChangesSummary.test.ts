import { afterEach, describe, expect, it } from "vitest";
import { stringify as toYaml } from "yaml";

import type { BlockYAML } from "@/routes/workflows/types/workflowYamlTypes";
import type { WorkflowSaveData } from "@/store/WorkflowHasChangesStore";
import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";

import {
  isDraftDirty,
  snapshotOf,
  summarizeBlockChanges,
  summarizeWorkflowChanges,
} from "./workflowChangesSummary";

const block = (label: string, extra: Record<string, unknown> = {}): BlockYAML =>
  ({ label, block_type: "task", ...extra }) as unknown as BlockYAML;

type SaveOver = {
  title?: string;
  blocks?: Array<BlockYAML>;
  parameters?: Array<
    Record<string, unknown> & { key: string; parameter_type: string }
  >;
  settings?: Record<string, unknown>;
};

const saveData = (over: SaveOver = {}): WorkflowSaveData =>
  ({
    title: over.title ?? "T",
    blocks: over.blocks ?? [],
    parameters: over.parameters ?? [],
    settings: over.settings ?? { proxyLocation: "RESIDENTIAL" },
    workflow: {
      title: "T",
      workflow_definition: { version: 2, blocks: [], parameters: [] },
    },
  }) as unknown as WorkflowSaveData;

describe("summarizeBlockChanges", () => {
  it("returns nothing when blocks are identical (no phantom edits)", () => {
    const blocks = [block("a", { url: "https://x" }), block("b")];
    expect(summarizeBlockChanges(blocks, blocks)).toEqual([]);
  });

  it("ignores cosmetic empty-value and key-order differences", () => {
    const baseline = [
      block("a", { url: "https://x", navigation_goal: null, data_schema: [] }),
    ];
    const draft = [block("a", { navigation_goal: "", url: "https://x" })];
    expect(summarizeBlockChanges(baseline, draft)).toEqual([]);
  });

  it("detects an added block with its type", () => {
    expect(
      summarizeBlockChanges(
        [block("a")],
        [block("a"), block("b", { block_type: "for_loop" })],
      ),
    ).toEqual([`Added for loop block "b"`]);
  });

  it("detects a removed block", () => {
    expect(
      summarizeBlockChanges([block("a"), block("b")], [block("a")]),
    ).toEqual([`Removed block "b"`]);
  });

  it("detects an edited block from a field change", () => {
    expect(
      summarizeBlockChanges(
        [block("a", { url: "https://x" })],
        [block("a", { url: "https://y" })],
      ),
    ).toEqual([`Edited block "a"`]);
  });

  it("detects a pure rename (same content, new label)", () => {
    expect(
      summarizeBlockChanges(
        [block("old", { url: "https://x" })],
        [block("new", { url: "https://x" })],
      ),
    ).toEqual([`Renamed block "old" to "new"`]);
  });

  it("keeps remove + add when a rename also changes content", () => {
    const out = summarizeBlockChanges(
      [block("old", { url: "https://x" })],
      [block("new", { url: "https://y" })],
    );
    expect(out).toContain(`Added task block "new"`);
    expect(out).toContain(`Removed block "old"`);
    expect(out).not.toContain(`Renamed block "old" to "new"`);
  });

  it("renames a loop container without flagging its unchanged children", () => {
    const loop = (label: string): BlockYAML =>
      ({
        label,
        block_type: "for_loop",
        loop_blocks: [block("child", { url: "https://x" })],
      }) as unknown as BlockYAML;
    // Renaming the container must not report its byte-identical child as
    // removed + added (the ancestor-label cascade bug).
    expect(summarizeBlockChanges([loop("loop1")], [loop("loop2")])).toEqual([
      `Renamed block "loop1" to "loop2"`,
    ]);
  });

  it("detects a rename of a nested loop child", () => {
    const loop = (childLabel: string): BlockYAML =>
      ({
        label: "loop",
        block_type: "for_loop",
        loop_blocks: [block(childLabel, { url: "https://x" })],
      }) as unknown as BlockYAML;
    expect(summarizeBlockChanges([loop("a")], [loop("b")])).toEqual([
      `Renamed block "a" to "b"`,
    ]);
  });

  it("flags only the edited loop child, not its container", () => {
    const container = (childUrl: string): BlockYAML =>
      ({
        label: "loop",
        block_type: "for_loop",
        loop_blocks: [block("child", { url: childUrl })],
      }) as unknown as BlockYAML;
    expect(
      summarizeBlockChanges([container("https://x")], [container("https://y")]),
    ).toEqual([`Edited block "child"`]);
  });

  it("tracks a loop child that shares a label with its container", () => {
    const loopWith = (children: Array<BlockYAML>): BlockYAML =>
      ({
        label: "dup",
        block_type: "for_loop",
        loop_blocks: children,
      }) as unknown as BlockYAML;
    // A global-label map would let the child overwrite the container and
    // misreport this as an edit; nesting-path keys keep them distinct.
    expect(
      summarizeBlockChanges(
        [loopWith([block("dup", { code: "x" })])],
        [loopWith([])],
      ),
    ).toEqual([`Removed block "dup"`]);
  });
});

describe("summarizeWorkflowChanges (snapshot baseline)", () => {
  it("returns [] when there is no snapshot yet (no user edit since load)", () => {
    expect(
      summarizeWorkflowChanges(saveData({ blocks: [block("a")] }), null),
    ).toEqual([]);
  });

  // The QA repro: a freshly loaded "Skyvern Page Checker" with a login block
  // whose credential goal the canvas auto-materialized on open. The baseline is
  // captured from the same live draft, so it lists nothing — no phantom
  // "Edited block block_1". Covers a login block with a credential parameter.
  it("lists nothing for a fresh load, including an autofilled login block", () => {
    const data = saveData({
      title: "Skyvern Page Checker",
      blocks: [
        block("start", { url: "https://example.com" }),
        block("block_1", {
          block_type: "login",
          parameter_keys: ["credentials"],
          navigation_goal: "log in using the provided context",
        }),
      ],
      parameters: [{ key: "credentials", parameter_type: "credential" }],
    });
    expect(summarizeWorkflowChanges(data, snapshotOf(data))).toEqual([]);
  });

  it("detects a real block edit against the snapshot", () => {
    const base = snapshotOf(saveData({ blocks: [block("a", { url: "x" })] }));
    expect(
      summarizeWorkflowChanges(
        saveData({ blocks: [block("a", { url: "y" })] }),
        base,
      ),
    ).toEqual([`Edited block "a"`]);
  });

  it("reports a workflow rename", () => {
    const base = snapshotOf(saveData({ title: "Old" }));
    expect(
      summarizeWorkflowChanges(saveData({ title: "New" }), base),
    ).toContain(`Renamed workflow to "New"`);
  });

  it("reports added/removed workflow parameters and ignores secrets", () => {
    const base = snapshotOf(
      saveData({
        parameters: [
          { key: "kept", parameter_type: "workflow" },
          { key: "gone", parameter_type: "workflow" },
        ],
      }),
    );
    const out = summarizeWorkflowChanges(
      saveData({
        parameters: [
          { key: "kept", parameter_type: "workflow" },
          { key: "added", parameter_type: "workflow" },
          { key: "secret", parameter_type: "aws_secret" },
        ],
      }),
      base,
    );
    expect(out).toContain(`Added parameter "added"`);
    expect(out).toContain(`Removed parameter "gone"`);
    expect(out).not.toContain(`Added parameter "secret"`);
  });

  const wfParam = (over: Record<string, unknown> = {}) => ({
    key: "param_x",
    parameter_type: "workflow",
    workflow_parameter_type: "string",
    ...over,
  });

  it("flags a default-value change as an edited parameter", () => {
    const base = snapshotOf(saveData({ parameters: [wfParam()] }));
    expect(
      summarizeWorkflowChanges(
        saveData({ parameters: [wfParam({ default_value: "y" })] }),
        base,
      ),
    ).toEqual([`Edited parameter "param_x"`]);
  });

  it("flags a description-only change as an edited parameter", () => {
    const base = snapshotOf(
      saveData({ parameters: [wfParam({ description: "a" })] }),
    );
    expect(
      summarizeWorkflowChanges(
        saveData({ parameters: [wfParam({ description: "b" })] }),
        base,
      ),
    ).toEqual([`Edited parameter "param_x"`]);
  });

  it("emits one line per edited parameter even when several fields change", () => {
    const base = snapshotOf(
      saveData({
        parameters: [wfParam({ default_value: "y", description: "a" })],
      }),
    );
    expect(
      summarizeWorkflowChanges(
        saveData({
          parameters: [
            wfParam({
              default_value: "z",
              description: "b",
              workflow_parameter_type: "integer",
            }),
          ],
        }),
        base,
      ),
    ).toEqual([`Edited parameter "param_x"`]);
  });

  it("emits nothing when a parameter is unchanged (no false positive)", () => {
    const data = saveData({
      parameters: [wfParam({ default_value: "y", description: "d" })],
    });
    expect(summarizeWorkflowChanges(data, snapshotOf(data))).toEqual([]);
  });

  it("names a proxy-location change instead of the generic bucket", () => {
    const base = snapshotOf(
      saveData({ settings: { proxyLocation: "RESIDENTIAL" } }),
    );
    expect(
      summarizeWorkflowChanges(
        saveData({ settings: { proxyLocation: "US-CA" } }),
        base,
      ),
    ).toEqual(["Changed proxy location"]);
  });

  it("names a boolean setting toggle", () => {
    const base = snapshotOf(
      saveData({ settings: { persistBrowserSession: false } }),
    );
    expect(
      summarizeWorkflowChanges(
        saveData({ settings: { persistBrowserSession: true } }),
        base,
      ),
    ).toEqual(["Toggled persist browser session"]);
  });

  it("groups a browser-profile change into one line", () => {
    const base = snapshotOf(
      saveData({
        settings: { browserProfileId: "p1", browserProfileKey: "k1" },
      }),
    );
    expect(
      summarizeWorkflowChanges(
        saveData({
          settings: { browserProfileId: "p2", browserProfileKey: "k2" },
        }),
        base,
      ),
    ).toEqual(["Changed browser profile"]);
  });

  it("groups sequential-run settings into one line", () => {
    const base = snapshotOf(
      saveData({ settings: { runSequentially: false, sequentialKey: null } }),
    );
    expect(
      summarizeWorkflowChanges(
        saveData({ settings: { runSequentially: true, sequentialKey: "k" } }),
        base,
      ),
    ).toEqual(["Changed sequential run settings"]);
  });

  it("emits nothing when a fully-populated settings object is unchanged", () => {
    const settings = {
      proxyLocation: "RESIDENTIAL",
      webhookCallbackUrl: "https://h",
      persistBrowserSession: true,
      pinSavedSessionIp: false,
      browserProfileId: "p",
      browserProfileKey: "k",
      model: null,
      maxScreenshotScrolls: 3,
      maxElapsedTimeMinutes: 10,
      extraHttpHeaders: '{"a":"b"}',
      cdpConnectHeaders: null,
      runWith: "code",
      codeVersion: 2,
      scriptCacheKey: "c",
      aiFallback: true,
      enableSelfHealing: false,
      runSequentially: true,
      sequentialKey: "s",
      finallyBlockLabel: "f",
      workflowSystemPrompt: "prompt",
      errorCodeMapping: { e: "m" },
    };
    const data = saveData({ settings });
    expect(summarizeWorkflowChanges(data, snapshotOf(data))).toEqual([]);
  });

  it("names every edit in a kitchen-sink change with no generic bucket", () => {
    const base = snapshotOf(
      saveData({
        title: "Old",
        blocks: [block("a", { url: "https://x" })],
        parameters: [wfParam({ default_value: "1" })],
        settings: { proxyLocation: "RESIDENTIAL", model: null },
      }),
    );
    const out = summarizeWorkflowChanges(
      saveData({
        title: "New",
        blocks: [block("a", { url: "https://y" })],
        parameters: [wfParam({ default_value: "2" })],
        settings: { proxyLocation: "US-CA", model: { modelName: "gpt" } },
      }),
      base,
    );
    expect(out).toContain(`Renamed workflow to "New"`);
    expect(out).toContain(`Edited block "a"`);
    expect(out).toContain(`Edited parameter "param_x"`);
    expect(out).toContain("Changed proxy location");
    expect(out).toContain("Changed model");
    expect(out).not.toContain("Other workflow changes");
  });

  it("uses the generic bucket only for a change no summarizer covers", () => {
    // A non-workflow (credential) parameter edit that doesn't also touch a
    // block is deliberately not itemized, so it falls to the catch-all.
    const base = snapshotOf(
      saveData({
        parameters: [
          { key: "c", parameter_type: "credential", credential_id: "a" },
        ],
      }),
    );
    expect(
      summarizeWorkflowChanges(
        saveData({
          parameters: [
            { key: "c", parameter_type: "credential", credential_id: "b" },
          ],
        }),
        base,
      ),
    ).toEqual(["Other workflow changes"]);
  });
});

describe("isDraftDirty", () => {
  it("is false with no snapshot", () => {
    expect(isDraftDirty(saveData({ blocks: [block("a")] }), null)).toBe(false);
  });

  it("is false when the draft matches the snapshot", () => {
    const data = saveData({ blocks: [block("a", { url: "x" })] });
    expect(isDraftDirty(data, snapshotOf(data))).toBe(false);
  });

  it("is true when a block changed", () => {
    const base = snapshotOf(saveData({ blocks: [block("a", { url: "x" })] }));
    expect(
      isDraftDirty(saveData({ blocks: [block("a", { url: "y" })] }), base),
    ).toBe(true);
  });
});

describe("summarizeWorkflowChanges YAML draft path", () => {
  afterEach(() => {
    useWorkflowYamlEditorStore.setState({
      active: false,
      draft: "",
      entrySnapshot: "",
    });
  });

  it("diffs the parsed YAML draft against the snapshot", () => {
    const canvas = saveData({
      blocks: [block("a", { block_type: "code", code: "# a" })],
    });
    const snap = snapshotOf(canvas); // captured while the YAML editor is closed
    useWorkflowYamlEditorStore.setState({
      active: true,
      entrySnapshot: toYaml({ parameters: [], blocks: canvas.blocks }),
      draft: toYaml({
        parameters: [],
        blocks: [
          ...canvas.blocks,
          { label: "b", block_type: "code", code: "# b" },
        ],
      }),
    });
    expect(summarizeWorkflowChanges(canvas, snap)).toContain(
      `Added code block "b"`,
    );
  });
});
