import { describe, expect, it } from "vitest";
import { dropNoiseExceptions } from "./posthogNoiseFilter";

const baseEvent = {
  uuid: "test",
  event: "$exception",
  properties: {} as Record<string, unknown>,
};

describe("dropNoiseExceptions", () => {
  it("drops ResizeObserver loop $exception events", () => {
    const result = dropNoiseExceptions({
      ...baseEvent,
      properties: {
        $exception_list: [
          {
            value:
              "ResizeObserver loop completed with undelivered notifications.",
          },
        ],
      },
    });
    expect(result).toBeNull();
  });

  it("keeps other $exception events", () => {
    const event = {
      ...baseEvent,
      properties: {
        $exception_list: [{ value: "TypeError: foo" }],
      },
    };
    expect(dropNoiseExceptions(event)).toBe(event);
  });

  it("keeps non-exception events", () => {
    const event = {
      ...baseEvent,
      event: "$pageview",
    };
    expect(dropNoiseExceptions(event)).toBe(event);
  });

  it("keeps events with empty exception list", () => {
    const event = {
      ...baseEvent,
      properties: { $exception_list: [] },
    };
    expect(dropNoiseExceptions(event)).toBe(event);
  });

  it("passes null through", () => {
    expect(dropNoiseExceptions(null)).toBeNull();
  });
});
