import { BrowserProfileApiResponse, BrowserProfileUsage } from "@/api/types";

// Two state nouns only, per the Browser Memory copy rules: a "remembered browser"
// (workflow memory / credential login, kept current automatically) vs a read-only
// "saved profile".
export type BrowserProfileRole = "workflow_memory" | "credential" | "plain";

// Derive from whichever credential signal the caller has: list rows pass the batched
// `linked_credential_name` (no per-row usage fetch); the delete/refresh dialogs pass the
// full `usage` they fetch on open.
export function getBrowserProfileRole(
  profile: Pick<
    BrowserProfileApiResponse,
    "is_managed" | "linked_credential_name"
  >,
  usage?: BrowserProfileUsage,
): BrowserProfileRole {
  if (profile.is_managed) {
    return "workflow_memory";
  }
  if (
    profile.linked_credential_name ||
    (usage && usage.credentials.length > 0)
  ) {
    return "credential";
  }
  return "plain";
}

export const BROWSER_PROFILE_ROLE_BADGE: Record<BrowserProfileRole, string> = {
  workflow_memory: "Workflow memory",
  credential: "Credential login",
  plain: "Saved profile",
};

export const BROWSER_PROFILE_ROLE_FRESHNESS: Record<
  BrowserProfileRole,
  string
> = {
  workflow_memory: "Updated after every successful run.",
  credential: "Kept fresh automatically.",
  plain: "Read-only saved profile.",
};

// Per-role delete warning. The dialog fetches full usage on open, so the credential name and any
// pinning workflows come from there; block nothing, warn everything.
export function deleteWarning(
  profile: Pick<
    BrowserProfileApiResponse,
    "is_managed" | "linked_credential_name"
  >,
  usage: BrowserProfileUsage | undefined,
): string {
  const role = getBrowserProfileRole(profile, usage);
  if (role === "workflow_memory") {
    return "This clears the workflow's remembered browser. Its next run starts fresh and saves a new one.";
  }
  if (role === "credential") {
    const names = usage?.credentials.map((c) => c.name).join(", ");
    return `This unlinks the saved login from ${names}. That credential will sign in fresh next time.`;
  }
  if ((usage?.workflows.length ?? 0) > 0) {
    return "Workflows pinned to this profile won't find it until they're repointed.";
  }
  return "This saved browser profile will be deleted and can't be recovered.";
}
