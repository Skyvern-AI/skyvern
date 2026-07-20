// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  BrowserProfileApiResponse,
  BrowserProfileUsage,
} from "@/api/types";

import { DeleteBrowserProfileButton } from "./DeleteBrowserProfileButton";
import { deleteWarning } from "./browserProfileRole";
import { useBrowserProfileUsageQuery } from "./hooks/useBrowserProfileUsageQuery";

vi.mock("./hooks/useBrowserProfileUsageQuery", () => ({
  useBrowserProfileUsageQuery: vi.fn(),
}));
vi.mock("./hooks/useBrowserProfileMutations", () => ({
  useDeleteBrowserProfileMutation: () => ({
    mutateAsync: vi.fn(),
    isPending: false,
  }),
}));

function makeProfile(
  overrides: Partial<BrowserProfileApiResponse> = {},
): BrowserProfileApiResponse {
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
    ...overrides,
  };
}

function usage(over: Partial<BrowserProfileUsage> = {}): BrowserProfileUsage {
  return {
    workflows: [],
    credentials: [],
    recent_seeded_run_count: 0,
    ...over,
  };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("deleteWarning copy selection", () => {
  it("warns that a managed profile is the workflow's memory", () => {
    expect(deleteWarning(makeProfile({ is_managed: true }), usage())).toContain(
      "clears the workflow's remembered browser",
    );
  });

  it("warns a credential-linked delete unlinks the saved login, by name", () => {
    const warning = deleteWarning(
      makeProfile(),
      usage({ credentials: [{ credential_id: "c", name: "Bank portal" }] }),
    );
    expect(warning).toContain("unlinks the saved login from Bank portal");
  });

  it("warns pinned workflows will need repointing for a plain profile", () => {
    const warning = deleteWarning(
      makeProfile(),
      usage({
        workflows: [
          { workflow_permanent_id: "w", title: "T", via: "browser_profile_id" },
        ],
      }),
    );
    expect(warning).toContain("Workflows pinned to this profile");
  });

  it("falls back to a plain unrecoverable-delete warning", () => {
    expect(deleteWarning(makeProfile(), usage())).toContain(
      "can't be recovered",
    );
  });
});

describe("DeleteBrowserProfileButton usage gating", () => {
  function open() {
    render(<DeleteBrowserProfileButton profile={makeProfile()} />);
    fireEvent.click(screen.getByLabelText("Delete browser profile"));
  }

  it("disables Delete while the used-by list is still loading", () => {
    vi.mocked(useBrowserProfileUsageQuery).mockReturnValue({
      data: undefined,
      isLoading: true,
    } as unknown as ReturnType<typeof useBrowserProfileUsageQuery>);
    open();
    const button = screen.getByRole("button", {
      name: "Delete",
    }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
  });

  it("enables Delete once usage has loaded", () => {
    vi.mocked(useBrowserProfileUsageQuery).mockReturnValue({
      data: usage(),
      isLoading: false,
    } as unknown as ReturnType<typeof useBrowserProfileUsageQuery>);
    open();
    const button = screen.getByRole("button", {
      name: "Delete",
    }) as HTMLButtonElement;
    expect(button.disabled).toBe(false);
  });
});
