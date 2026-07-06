import { renderHook } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { useSidebarHidden } from "./useSidebarHidden";

function wrapper(initialEntry: string) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return (
      <MemoryRouter initialEntries={[initialEntry]}>{children}</MemoryRouter>
    );
  };
}

describe("useSidebarHidden", () => {
  it("hides the sidebar on editor routes under /agents", () => {
    for (const path of [
      "/agents/wpid_1/edit",
      "/agents/wpid_1/studio",
      "/agents/wpid_1/build",
      "/agents/wpid_1/wr_1/Login/build",
      "/agents/wpid_1/debug",
    ]) {
      const { result } = renderHook(() => useSidebarHidden(), {
        wrapper: wrapper(path),
      });
      expect(result.current).toBe(true);
    }
  });

  it("hides the sidebar at the legacy /workflows alias so nothing flashes mid-redirect", () => {
    for (const path of ["/workflows/wpid_1/edit", "/workflows/wpid_1/studio"]) {
      const { result } = renderHook(() => useSidebarHidden(), {
        wrapper: wrapper(path),
      });
      expect(result.current).toBe(true);
    }
  });

  it("keeps the sidebar on list and run pages", () => {
    for (const path of ["/agents", "/agents/wpid_1/runs", "/runs"]) {
      const { result } = renderHook(() => useSidebarHidden(), {
        wrapper: wrapper(path),
      });
      expect(result.current).toBe(false);
    }
  });

  it("hides the sidebar for embedded views regardless of route", () => {
    const { result } = renderHook(() => useSidebarHidden(), {
      wrapper: wrapper("/agents?embed=true"),
    });
    expect(result.current).toBe(true);
  });
});
