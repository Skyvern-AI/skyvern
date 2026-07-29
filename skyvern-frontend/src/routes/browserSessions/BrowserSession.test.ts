import { describe, expect, it } from "vitest";

import { getBrowserSessionTabFromPathname } from "./BrowserSession.utils";

describe("getBrowserSessionTabFromPathname", () => {
  it.each([
    ["/browser-session/session-1/timeline", "timeline"],
    ["/browser-session/session-1/downloads", "downloads"],
  ] as const)("maps %s to %s", (pathname, tab) => {
    expect(getBrowserSessionTabFromPathname(pathname)).toBe(tab);
  });
});
