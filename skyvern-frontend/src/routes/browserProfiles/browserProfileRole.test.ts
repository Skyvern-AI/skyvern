import { describe, expect, it } from "vitest";

import type {
  BrowserProfileApiResponse,
  BrowserProfileUsage,
} from "@/api/types";

import { getBrowserProfileRole } from "./browserProfileRole";

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
