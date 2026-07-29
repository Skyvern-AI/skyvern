import { describe, expect, it } from "vitest";

import type {
  BrowserProfileApiResponse,
  BrowserProfileUsage,
} from "@/api/types";

import {
  BROWSER_PROFILE_ROLE_BADGE,
  BROWSER_PROFILE_ROLE_TOOLTIP,
  deleteWarning,
  getBrowserProfileRole,
} from "./browserProfileRole";

const managed = { is_managed: true } as Pick<
  BrowserProfileApiResponse,
  "is_managed"
>;
const plain = { is_managed: false } as Pick<
  BrowserProfileApiResponse,
  "is_managed"
>;

function usage(
  overrides: Partial<BrowserProfileUsage> = {},
): BrowserProfileUsage {
  return {
    workflows: [],
    credentials: [],
    recent_seeded_run_count: 0,
    ...overrides,
  };
}

describe("getBrowserProfileRole", () => {
  it("is workflow_memory for a managed profile regardless of usage", () => {
    expect(getBrowserProfileRole(managed, undefined)).toBe("workflow_memory");
    expect(
      getBrowserProfileRole(
        managed,
        usage({ credentials: [{ credential_id: "c", name: "n" }] }),
      ),
    ).toBe("workflow_memory");
  });

  it("is credential when a non-managed profile is linked by a credential", () => {
    expect(
      getBrowserProfileRole(
        plain,
        usage({ credentials: [{ credential_id: "c", name: "Bank" }] }),
      ),
    ).toBe("credential");
  });

  it("is credential from the list's linked_credential_name without a usage fetch", () => {
    expect(
      getBrowserProfileRole({
        is_managed: false,
        linked_credential_name: "Bank portal",
      }),
    ).toBe("credential");
  });

  it("is plain for a non-managed profile with no credential link", () => {
    expect(getBrowserProfileRole(plain, usage())).toBe("plain");
    expect(getBrowserProfileRole(plain, undefined)).toBe("plain");
  });
});

describe("role badges + delete copy", () => {
  it("badges are the bare agent/credential/user nouns", () => {
    expect(BROWSER_PROFILE_ROLE_BADGE).toEqual({
      workflow_memory: "agent",
      credential: "credential",
      plain: "user",
    });
  });

  it("every role has an explanatory tooltip", () => {
    expect(BROWSER_PROFILE_ROLE_TOOLTIP.workflow_memory).toContain("agent");
    expect(BROWSER_PROFILE_ROLE_TOOLTIP.credential).toContain("credential");
    expect(BROWSER_PROFILE_ROLE_TOOLTIP.plain).toContain("you");
  });

  it("delete warnings use the agent noun, never workflow", () => {
    expect(deleteWarning(managed, usage())).toContain("agent");
    expect(deleteWarning(managed, usage())).not.toMatch(/workflow/i);
    const pinned = deleteWarning(
      plain,
      usage({
        workflows: [
          { workflow_permanent_id: "w", title: "T", via: "browser_profile_id" },
        ],
      }),
    );
    expect(pinned).toContain("Agents pinned");
  });
});
