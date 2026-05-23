import { describe, expect, test } from "vitest";

import { formatDropBlockedToast } from "./dropBlockedToast";

describe("formatDropBlockedToast (SKY-9062)", () => {
  describe("forward-reference", () => {
    test("names the moved block and lists each referrer once", () => {
      // The AC requires the toast to be specific enough that the user knows
      // how to fix the violation — both the moved block and the referring
      // blocks must appear in the copy.
      const content = formatDropBlockedToast({
        kind: "forward-reference",
        movedBlockLabel: "fetch_profile",
        referrerLabels: ["send_email", "log_event"],
      });
      expect(content.title).toBe(
        "Can't reorder: would create a forward reference",
      );
      expect(content.description).toContain('"fetch_profile"');
      expect(content.details).toEqual(["send_email", "log_event"]);
    });

    test("dedupes duplicate referrer labels so the list stays readable", () => {
      // `findForwardReferenceViolations` emits one entry per referring
      // field, so the same block can appear multiple times when it
      // references the moved block from several parameters. The toast
      // dedupes so the user sees each offending block once.
      const content = formatDropBlockedToast({
        kind: "forward-reference",
        movedBlockLabel: "a",
        referrerLabels: ["b", "b", "c", "b"],
      });
      expect(content.details).toEqual(["b", "c"]);
    });

    test("handles the single-referrer case", () => {
      const content = formatDropBlockedToast({
        kind: "forward-reference",
        movedBlockLabel: "x",
        referrerLabels: ["y"],
      });
      expect(content.details).toEqual(["y"]);
    });
  });

  describe("finally-pin", () => {
    test("names the finally block and explains the pinning invariant", () => {
      const content = formatDropBlockedToast({
        kind: "finally-pin",
        finallyBlockLabel: "cleanup",
      });
      expect(content.title).toBe("Can't reorder: finally block must run last");
      expect(content.description).toContain('"cleanup"');
      expect(content.description.toLowerCase()).toContain("last");
      expect(content.details).toEqual([]);
    });
  });

  describe("drag-mode", () => {
    test("recording reason tells the user to stop recording", () => {
      // Matches the tooltip text surfaced on the grip handle so both
      // affordances converge on the same fix (`getDragGateReason`).
      const content = formatDropBlockedToast({
        kind: "drag-mode",
      });
      expect(content.title).toContain("recording");
      expect(content.description).toBe("Stop recording to reorder blocks.");
      expect(content.details).toEqual([]);
    });
  });

  describe("cross-scope", () => {
    test("names the moved block and explains the scope rule", () => {
      // The AC: reason text is specific enough that a user knows how to
      // fix it. For cross-scope, the fix is "drop inside the same group",
      // which the description spells out (loops + conditional branches).
      const content = formatDropBlockedToast({
        kind: "cross-scope",
        movedBlockLabel: "http_call",
      });
      expect(content.title).toBe(
        "Can't reorder: drop target is outside this group",
      );
      expect(content.description).toContain('"http_call"');
      expect(content.description.toLowerCase()).toContain("loop");
      expect(content.details).toEqual([]);
    });
  });

  describe("title prefix convention", () => {
    test("every reason starts with the same Can't reorder prefix", () => {
      // The AC collapses all drop-block paths into one toast component;
      // keeping the title prefix identical lets users recognise the
      // constraint class at a glance (especially helpful for the polite
      // aria-live channel, which doesn't interrupt).
      const reasons = [
        formatDropBlockedToast({
          kind: "forward-reference",
          movedBlockLabel: "x",
          referrerLabels: ["y"],
        }),
        formatDropBlockedToast({
          kind: "finally-pin",
          finallyBlockLabel: "cleanup",
        }),
        formatDropBlockedToast({
          kind: "drag-mode",
        }),
        formatDropBlockedToast({
          kind: "cross-scope",
          movedBlockLabel: "x",
        }),
      ];
      for (const content of reasons) {
        expect(content.title.startsWith("Can't reorder")).toBe(true);
      }
    });
  });
});
