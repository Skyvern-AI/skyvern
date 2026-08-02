import { describe, expect, test } from "vitest";

import { resolveWorkspaceBrowserSessionBindings } from "./browserSessionBindings";

describe("resolveWorkspaceBrowserSessionBindings", () => {
  test("uses the active run only as the display session", () => {
    expect(
      resolveWorkspaceBrowserSessionBindings("pbs_debug", "pbs_run"),
    ).toEqual({
      debugBrowserSessionId: "pbs_debug",
      displayBrowserSessionId: "pbs_run",
    });
  });

  test("rebinds the display to the debug session after release", () => {
    expect(resolveWorkspaceBrowserSessionBindings("pbs_debug", null)).toEqual({
      debugBrowserSessionId: "pbs_debug",
      displayBrowserSessionId: "pbs_debug",
    });
  });
});
