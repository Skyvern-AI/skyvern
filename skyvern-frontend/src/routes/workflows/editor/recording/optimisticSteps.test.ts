import { describe, expect, it } from "vitest";

import type { MessageInExfiltratedConsoleEvent } from "@/store/useRecordingStore";

import { buildOptimisticStep } from "./optimisticSteps";

function consoleEvent(
  type: string,
  overrides: Partial<MessageInExfiltratedConsoleEvent["params"]> = {},
): MessageInExfiltratedConsoleEvent {
  return {
    kind: "exfiltrated-event",
    event_name: "user_interaction",
    source: "console",
    timestamp: 1.0,
    params: {
      type,
      url: "https://example.com",
      timestamp: 1000,
      target: { tagName: "BUTTON", text: ["Submit"] },
      mousePosition: { xa: null, ya: null, xp: 0.5, yp: 0.5 },
      activeElement: { tagName: "BUTTON" },
      window: { width: 1200, height: 800, scrollX: 0, scrollY: 0 },
      ...overrides,
    },
  };
}

describe("buildOptimisticStep", () => {
  it("builds a click placeholder from target text", () => {
    const step = buildOptimisticStep(consoleEvent("click"));
    expect(step?.action_kind).toBe("click");
    expect(step?.title).toBe("Click 'Submit'");
  });

  it("falls back through innerText then tag name for click text", () => {
    expect(
      buildOptimisticStep(
        consoleEvent("click", {
          target: { tagName: "DIV", text: [], innerText: "Read more" },
        }),
      )?.title,
    ).toBe("Click 'Read more'");

    expect(
      buildOptimisticStep(
        consoleEvent("click", { target: { tagName: "DIV", text: [] } }),
      )?.title,
    ).toBe("Click 'div'");
  });

  it("builds an input_text placeholder on change/blur with a value", () => {
    const changeStep = buildOptimisticStep(
      consoleEvent("change", {
        target: { tagName: "INPUT", text: ["Email"], value: "a@b.com" },
      }),
    );
    expect(changeStep?.action_kind).toBe("input_text");
    expect(changeStep?.title).toBe("Fill 'Email'");

    expect(
      buildOptimisticStep(
        consoleEvent("blur", {
          target: { tagName: "INPUT", text: ["Email"] },
          inputValue: "typed",
        }),
      )?.action_kind,
    ).toBe("input_text");
  });

  it("returns null for change/blur without a value", () => {
    expect(
      buildOptimisticStep(
        consoleEvent("blur", { target: { tagName: "INPUT", text: ["Email"] } }),
      ),
    ).toBeNull();
    expect(
      buildOptimisticStep(
        consoleEvent("change", {
          target: { tagName: "INPUT", text: ["Email"], value: "   " },
        }),
      ),
    ).toBeNull();
  });

  it("elides events that do not map to a step", () => {
    for (const type of ["focus", "input", "keydown", "keypress", "mousemove"]) {
      expect(buildOptimisticStep(consoleEvent(type))).toBeNull();
    }
  });

  it("elides navigation (cdp) events entirely", () => {
    const nav = {
      kind: "exfiltrated-event" as const,
      event_name: "nav:frame_navigated",
      source: "cdp" as const,
      timestamp: 2.0,
      params: { targetInfo: { url: "https://example.com/next" } },
    };
    expect(buildOptimisticStep(nav)).toBeNull();
  });
});
