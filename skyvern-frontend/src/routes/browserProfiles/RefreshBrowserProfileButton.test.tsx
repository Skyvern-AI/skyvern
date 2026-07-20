// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BrowserProfileApiResponse } from "@/api/types";

import { RefreshBrowserProfileButton } from "./RefreshBrowserProfileButton";

const mutate = vi.fn();

vi.mock(
  "@/routes/browserSessions/hooks/useCreateBrowserSessionMutation",
  () => ({
    useCreateBrowserSessionMutation: () => ({ mutate, isPending: false }),
  }),
);

vi.mock("./hooks/useBrowserProfileUsageQuery", () => ({
  useBrowserProfileUsageQuery: () => ({ data: undefined, isLoading: false }),
}));

function makeProfile(): BrowserProfileApiResponse {
  return {
    browser_profile_id: "bp_x",
    organization_id: "org_1",
    name: "My Profile",
    description: null,
    source_browser_type: null,
    is_managed: false,
    created_at: "2026-06-01T00:00:00.000Z",
    modified_at: "2026-06-01T00:00:00.000Z",
    deleted_at: null,
  };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("RefreshBrowserProfileButton", () => {
  it("seeds a browser session with the profile id on confirm", () => {
    render(<RefreshBrowserProfileButton profile={makeProfile()} />);

    fireEvent.click(screen.getByLabelText("Refresh browser profile"));
    fireEvent.click(screen.getByText("Open browser session"));

    expect(mutate).toHaveBeenCalledWith({
      browserProfileId: "bp_x",
      proxyLocation: null,
      proxySessionId: null,
      timeout: null,
    });
  });
});
