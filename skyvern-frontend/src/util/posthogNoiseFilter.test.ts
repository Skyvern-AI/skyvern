import { describe, expect, it } from "vitest";
import { dropNoiseExceptions } from "./posthogNoiseFilter";

const baseEvent = {
  uuid: "test",
  event: "$exception",
  properties: {} as Record<string, unknown>,
};

describe("dropNoiseExceptions", () => {
  it.each([
    "ResizeObserver loop completed with undelivered notifications.",
    "Non-Error promise rejection captured with value: Object Not Found Matching Id:5, MethodName:update, ParamCount:4",
  ])("drops known noise $exception events: %s", (value) => {
    const result = dropNoiseExceptions({
      ...baseEvent,
      properties: {
        $exception_list: [{ value }],
      },
    });
    expect(result).toBeNull();
  });

  it.each([
    "TypeError: foo",
    "Non-Error promise rejection captured with value: Object Not Found Matching Id:5, MethodName:update, ParamCount:4 extra",
  ])("keeps other $exception events: %s", (value) => {
    const event = {
      ...baseEvent,
      properties: {
        $exception_list: [{ value }],
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
