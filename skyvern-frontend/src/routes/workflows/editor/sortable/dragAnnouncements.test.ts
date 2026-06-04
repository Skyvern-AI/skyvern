import { describe, expect, test } from "vitest";

import {
  SCREEN_READER_INSTRUCTIONS,
  buildDragAnnouncements,
  resolveBlockLabel,
  type AnnouncementNode,
} from "./dragAnnouncements";

/**
 * SKY-9066 — the announcements module replaces dnd-kit's default id-based
 * messages with label-driven strings so VoiceOver / NVDA users hear a
 * recognisable block name for each drag phase. These tests guard the four
 * phases (pickup, move, drop, cancel), the fallback path when a block label
 * is missing, and the wording of the screen-reader instructions — which must
 * stay in sync with the keyboard sensor bindings so users are not told about
 * shortcuts that do not work.
 */

function makeNode(id: string, label?: string): AnnouncementNode {
  return { id, data: label === undefined ? undefined : { label } };
}

function makeActive(id: string) {
  return { active: { id } } as Parameters<
    NonNullable<ReturnType<typeof buildDragAnnouncements>["onDragStart"]>
  >[0];
}

function makeArgs(activeId: string, overId: string | null) {
  return {
    active: { id: activeId },
    over: overId === null ? null : { id: overId },
  } as Parameters<
    NonNullable<ReturnType<typeof buildDragAnnouncements>["onDragEnd"]>
  >[0];
}

describe("resolveBlockLabel (SKY-9066)", () => {
  test("returns the block label when set", () => {
    const nodes = [makeNode("n1", "Navigate to site")];
    expect(resolveBlockLabel(nodes, "n1")).toBe("Navigate to site");
  });

  test("falls back to the id when no label is configured", () => {
    // In practice every block has a label — but a silent empty string would
    // produce announcements like "Picked up workflow block ." which is
    // worse than a raw id for screen-reader users, so the fallback is
    // intentional.
    const nodes = [makeNode("n1")];
    expect(resolveBlockLabel(nodes, "n1")).toBe("n1");
  });

  test("falls back to the id when the node is missing from the list", () => {
    // The announcements are built once per FlowRenderer render and reused by
    // each phase callback. A drop can still fire with an id that has just
    // been removed from the nodes array (for example after a concurrent
    // delete). Returning the raw id keeps the announcement informative
    // instead of silently truncating.
    expect(resolveBlockLabel([], "missing")).toBe("missing");
  });

  test("ignores non-string-like ids", () => {
    expect(resolveBlockLabel([], undefined)).toBe("");
    expect(resolveBlockLabel([], null)).toBe("");
    expect(resolveBlockLabel([], {})).toBe("");
  });
});

describe("buildDragAnnouncements (SKY-9066)", () => {
  const nodes = [
    makeNode("n1", "Download report"),
    makeNode("n2", "Login step"),
    makeNode("n3", "Upload result"),
  ];

  test("onDragStart announces pickup by block label", () => {
    const announcements = buildDragAnnouncements(nodes);
    expect(announcements.onDragStart(makeActive("n1"))).toBe(
      "Picked up workflow block Download report.",
    );
  });

  test("onDragOver announces the drop target when hovering a sibling", () => {
    // The over event is how screen-reader users follow a keyboard-driven
    // drag: without this string they can press ↑ / ↓ repeatedly and never
    // know whether the block has moved. Naming both blocks is the only way
    // to resolve ambiguity when several blocks share a type.
    const announcements = buildDragAnnouncements(nodes);
    expect(announcements.onDragOver(makeArgs("n1", "n2"))).toBe(
      "Workflow block Download report is over Login step.",
    );
  });

  test("onDragOver announces when the active block leaves every drop target", () => {
    // dnd-kit fires onDragOver with `over: null` when the pointer / keyboard
    // focus leaves the sortable region entirely. A silent null would mean
    // the user stops hearing feedback and cannot tell whether the block is
    // still picked up.
    const announcements = buildDragAnnouncements(nodes);
    expect(announcements.onDragOver(makeArgs("n1", null))).toBe(
      "Workflow block Download report is no longer over a drop target.",
    );
  });

  test("onDragEnd announces the final drop target", () => {
    const announcements = buildDragAnnouncements(nodes);
    expect(announcements.onDragEnd(makeArgs("n1", "n3"))).toBe(
      "Workflow block Download report was dropped onto Upload result.",
    );
  });

  test("onDragEnd announces a drop with no target (keyboard release outside any slot)", () => {
    const announcements = buildDragAnnouncements(nodes);
    expect(announcements.onDragEnd(makeArgs("n1", null))).toBe(
      "Workflow block Download report was dropped.",
    );
  });

  test("onDragCancel announces that the block returned to its original position", () => {
    // Escape during a keyboard drag is silent in dnd-kit's defaults, which
    // leaves screen-reader users unsure whether the cancel worked. The
    // explicit "returned to its original position" phrasing is the single
    // cue that distinguishes cancel from drop.
    const announcements = buildDragAnnouncements(nodes);
    expect(announcements.onDragCancel(makeArgs("n1", "n2"))).toBe(
      "Drag cancelled. Workflow block Download report returned to its original position.",
    );
  });

  test("announcements rebuild against the latest nodes array", () => {
    // FlowRenderer rebuilds the announcement object on every render so the
    // post-reorder chain is visible to `onDragEnd`. Verify the builder
    // actually closes over its argument rather than caching a module-level
    // node list.
    const before = buildDragAnnouncements(nodes);
    const renamed = nodes.map((node) =>
      node.id === "n1" ? makeNode("n1", "Updated label") : node,
    );
    const after = buildDragAnnouncements(renamed);
    expect(before.onDragStart(makeActive("n1"))).toContain("Download report");
    expect(after.onDragStart(makeActive("n1"))).toContain("Updated label");
  });
});

describe("SCREEN_READER_INSTRUCTIONS (SKY-9066)", () => {
  test("names the Space / Arrow / Escape shortcuts wired in dragSensors", () => {
    // Keep this test in sync with `dragSensors.ts`: if the keyboard
    // coordinate getter changes or we add a new shortcut, update the
    // instructions string and this assertion together. Without that check
    // the screen-reader narration will drift out of sync with the actual
    // keyboard contract.
    const text = SCREEN_READER_INSTRUCTIONS.draggable;
    expect(text).toContain("Space");
    expect(text).toContain("Up and Down arrow");
    expect(text).toContain("Escape");
  });
});
