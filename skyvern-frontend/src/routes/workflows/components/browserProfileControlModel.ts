// Single source of truth for the merged browser-profile control: which stored
// field a pick writes to, and the locked captions (v26 mockup). A dropdown pick
// writes workflows.browser_profile_id (backend plan #13617 confirmed the single
// pick field; the seed column was dropped). Kept isolated here regardless.
import { BROWSER_PROFILE_ROLE_TOOLTIP } from "@/routes/browserProfiles/browserProfileRole";

export type ControlMode = "dropdown" | "code";

export type ControlFields = {
  browserProfileId: string | null;
  browserProfileKey: string | null;
};

export type ControlState = {
  mode: ControlMode;
  profileId: string | null;
  codeExpr: string | null;
};

export function controlStateFromFields(fields: ControlFields): ControlState {
  // Match the backend resolver, which gives an explicit browser_profile_id
  // precedence over browser_profile_key (service.py:1941). A legacy agent can
  // have both set from the old independent controls; showing the pick (not the
  // key) keeps the UI honest about which profile the run actually uses.
  if (fields.browserProfileId) {
    return {
      mode: "dropdown",
      profileId: fields.browserProfileId,
      codeExpr: null,
    };
  }
  if (fields.browserProfileKey) {
    return {
      mode: "code",
      profileId: null,
      codeExpr: fields.browserProfileKey,
    };
  }
  return {
    mode: "dropdown",
    profileId: null,
    codeExpr: null,
  };
}

export type ProfileRole = "plain" | "credential" | "workflow";

export function classifyProfileRole(profile: {
  is_managed?: boolean;
  linked_credential_name?: string | null;
}): ProfileRole {
  if (profile.is_managed) return "workflow";
  if (profile.linked_credential_name) return "credential";
  return "plain";
}

export function roleBadgeLabel(role: ProfileRole): string {
  if (role === "credential") return "credential";
  if (role === "workflow") return "agent";
  return "user";
}

// Reuse the profiles-page tooltip copy so the picker badge and the page badge
// speak one source (item 9 consolidation is still pending, but the tooltip text
// should not fork in the meantime).
const ROLE_TO_PAGE_KEY = {
  workflow: "workflow_memory",
  credential: "credential",
  plain: "plain",
} as const;

export function roleBadgeTooltip(role: ProfileRole): string {
  return BROWSER_PROFILE_ROLE_TOOLTIP[ROLE_TO_PAGE_KEY[role]];
}

function relativeTimeShort(iso: string | null | undefined): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const s = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (s < 60) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}d ago`;
  const mo = Math.floor(d / 30);
  if (mo < 12) return `${mo}mo ago`;
  return `${Math.floor(mo / 12)}y ago`;
}

// Derived dropdown-row subtitle (v27 §1): who keeps this profile + how fresh,
// from data the list rows already carry — so two similar names are tellable apart.
export function roleSubtitle(profile: {
  browser_profile_id: string;
  is_managed?: boolean;
  linked_credential_name?: string | null;
  modified_at?: string;
}): string {
  const rel = relativeTimeShort(profile.modified_at);
  const role = classifyProfileRole(profile);
  if (role === "credential") {
    return `Kept signed in by the credential${rel ? ` · sign-in ${rel}` : ""}`;
  }
  if (role === "workflow") {
    return `Updated by that agent${rel ? ` · run ${rel}` : ""}`;
  }
  const idShort = profile.browser_profile_id.slice(0, 7);
  return `${idShort}… · yours${rel ? ` · updated ${rel}` : ""}`;
}

export type CredScenario = "none" | "cred" | "crednp" | "rotation";

// Pure resolver for the login-block scenario that picks the Auto caption. Kept
// out of the hook so the branching is unit-testable without ReactFlow/store/query
// mocks. External-vault credentials have no Skyvern profile → treated as no login.
export function deriveCredScenario(input: {
  hasLoginNode: boolean;
  externalCredential: boolean;
  isRotating: boolean;
  hasSingleCredential: boolean;
  isFetched: boolean;
  credentialHasProfile: boolean;
}): CredScenario {
  if (!input.hasLoginNode || input.externalCredential) return "none";
  if (input.isRotating) return "rotation";
  if (!input.hasSingleCredential || !input.isFetched) return "crednp";
  return input.credentialHasProfile ? "cred" : "crednp";
}

// The resting sub-caption when nothing is picked, per login-block scenario (v32
// panels 1-2). No save concept — "None" is today's off; a login credential still
// brings its own profile via the fall-through, and rotation isolates per account.
export function autoCaption(
  scn: CredScenario,
  credName = "the credential",
): string {
  return {
    none: "Fresh browser every run",
    cred: `Starts signed in via credential “${credName}”`,
    crednp: `Fresh browser — the first sign-in saves credential “${credName}”’s profile`,
    rotation: `Starts signed in via credential “${credName}” — rotates per run when multiple`,
  }[scn];
}

// Caption under the control after a profile is picked (v32). One behavior: runs
// start from the pick and the last successful run updates it. Role only changes
// who the writer can be — the credential heals its own; an agent pick joins it.
export function pickCaption(
  role: ProfileRole,
  opts: { shared?: boolean } = {},
): string {
  if (role === "credential") {
    return "Runs start from this profile; the credential keeps it up to date.";
  }
  if (role === "workflow") {
    return "Runs start from this profile; the last successful run — yours or that agent’s — updates it.";
  }
  return opts.shared
    ? "Runs start from this profile; the last successful run — from any agent using it — updates it."
    : "Runs start from this profile; the last successful run updates it.";
}

// Per-input key caption (v32 panel 9): a profile per input value, each maintained
// by its own runs. No save concept — per-input always maintains its profiles.
export function codeCaption(): string {
  return "One profile per value of this input — each updated by its own runs.";
}

// A shared pick another agent also uses (v32 panels 4/8): non-blocking warning.
export function sharedWriteWarning(agentName: string): string {
  return `Agent “${agentName}” also uses this profile — runs overwrite each other’s saved state. Give each agent its own profile (＋ New profile) if they shouldn’t share.`;
}

// Rotating credentials but a single picked profile (v32 panel 10): non-blocking steer.
export function rotationWarning(credentialCount: number): string {
  return `This agent rotates ${credentialCount} credentials but saves every run into one profile — accounts will overwrite each other’s sessions. Use per-input {{ credentials }} for one profile per account.`;
}

// F6: the agent's login credential is pinned but the picked profile isn't — the
// run uses the credential's pin, so surface it read-only instead of an own-pin control.
export function credentialPinnedIpCaption(credName: string): string {
  return `IP pinned by credential “${credName}”`;
}

// F5: a persist-ON legacy workflow with no materialized pick row yet — show the
// derived own-profile pick instead of an empty picker that lies about the state.
export function virtualOwnProfileLabel(agentName: string): string {
  return `${agentName}’s profile`;
}

// F7: attaching a profile that agents already pick makes the credential its only
// writer (heal-only) — their runs stop saving into it. Non-blocking disclosure.
export function credentialAttachWarning(agentCount: number): string {
  const agents = agentCount === 1 ? "agent picks" : "agents pick";
  return `${agentCount} ${agents} this profile — attaching makes this credential its only writer; their runs will stop saving into it.`;
}
