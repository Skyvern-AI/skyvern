import { describe, expect, it } from "vitest";

import {
  autoCaption,
  classifyProfileRole,
  codeCaption,
  controlStateFromFields,
  credentialAttachWarning,
  credentialPinnedIpCaption,
  deriveCredScenario,
  pickCaption,
  roleBadgeLabel,
  roleBadgeTooltip,
  roleSubtitle,
  rotationWarning,
  sharedWriteWarning,
  virtualOwnProfileLabel,
} from "./browserProfileControlModel";

const scenarioInput = {
  hasLoginNode: true,
  externalCredential: false,
  isRotating: false,
  hasSingleCredential: true,
  isFetched: true,
  credentialHasProfile: false,
};

describe("deriveCredScenario", () => {
  it("no login node is 'none'", () => {
    expect(deriveCredScenario({ ...scenarioInput, hasLoginNode: false })).toBe(
      "none",
    );
  });
  it("an external-vault credential is 'none' (no Skyvern profile)", () => {
    expect(
      deriveCredScenario({ ...scenarioInput, externalCredential: true }),
    ).toBe("none");
  });
  it("two+ credentials is 'rotation'", () => {
    expect(deriveCredScenario({ ...scenarioInput, isRotating: true })).toBe(
      "rotation",
    );
  });
  it("a credential still loading is 'crednp'", () => {
    expect(deriveCredScenario({ ...scenarioInput, isFetched: false })).toBe(
      "crednp",
    );
  });
  it("a fetched credential with a saved profile is 'cred'", () => {
    expect(
      deriveCredScenario({ ...scenarioInput, credentialHasProfile: true }),
    ).toBe("cred");
  });
  it("a fetched credential without a profile is 'crednp'", () => {
    expect(deriveCredScenario(scenarioInput)).toBe("crednp");
  });
});

describe("control state from fields", () => {
  it("a picked profile is dropdown mode on browser_profile_id", () => {
    expect(
      controlStateFromFields({
        browserProfileId: "bp_1",
        browserProfileKey: null,
      }),
    ).toEqual({ mode: "dropdown", profileId: "bp_1", codeExpr: null });
  });

  it("a key alone is code mode", () => {
    expect(
      controlStateFromFields({
        browserProfileId: null,
        browserProfileKey: "{{ account_id }}",
      }),
    ).toEqual({ mode: "code", profileId: null, codeExpr: "{{ account_id }}" });
  });

  it("a pick wins over a key when both are set (matches backend precedence)", () => {
    expect(
      controlStateFromFields({
        browserProfileId: "bp_1",
        browserProfileKey: "{{ account_id }}",
      }),
    ).toEqual({ mode: "dropdown", profileId: "bp_1", codeExpr: null });
  });

  it("empty is Auto (dropdown, no id)", () => {
    expect(
      controlStateFromFields({
        browserProfileId: null,
        browserProfileKey: null,
      }),
    ).toEqual({ mode: "dropdown", profileId: null, codeExpr: null });
  });
});

describe("role classification + badges", () => {
  it("managed -> workflow, credential-linked -> credential, else plain", () => {
    expect(classifyProfileRole({ is_managed: true })).toBe("workflow");
    expect(
      classifyProfileRole({
        is_managed: false,
        linked_credential_name: "Acme Login",
      }),
    ).toBe("credential");
    expect(
      classifyProfileRole({ is_managed: false, linked_credential_name: null }),
    ).toBe("plain");
  });
  it("badge labels", () => {
    expect(roleBadgeLabel("workflow")).toBe("agent");
    expect(roleBadgeLabel("credential")).toBe("credential");
    expect(roleBadgeLabel("plain")).toBe("user");
  });
});

// v32 "one dropdown, no lock" — one model assertion per panel.
describe("v32 panels", () => {
  it("1 · none · no credential", () => {
    expect(autoCaption("none")).toBe("Fresh browser every run");
  });
  it("2 · none + credential (incl. rotation)", () => {
    expect(autoCaption("cred", "Kronos 5/15/26")).toBe(
      "Starts signed in via credential “Kronos 5/15/26”",
    );
    expect(autoCaption("rotation", "Kronos 5/15/26")).toBe(
      "Starts signed in via credential “Kronos 5/15/26” — rotates per run when multiple",
    );
  });
  it("3 · picked (your profile) — the one behavior", () => {
    expect(pickCaption("plain")).toBe(
      "Runs start from this profile; the last successful run updates it.",
    );
  });
  it("4 · picked + other agents pick it too — warning", () => {
    expect(pickCaption("plain", { shared: true })).toBe(
      "Runs start from this profile; the last successful run — from any agent using it — updates it.",
    );
    expect(sharedWriteWarning("Inventory Sync")).toBe(
      "Agent “Inventory Sync” also uses this profile — runs overwrite each other’s saved state. Give each agent its own profile (＋ New profile) if they shouldn’t share.",
    );
  });
  it("7 · credential-owned pick", () => {
    expect(pickCaption("credential")).toBe(
      "Runs start from this profile; the credential keeps it up to date.",
    );
  });
  it("8 · another agent's profile pick — joining", () => {
    expect(pickCaption("workflow")).toBe(
      "Runs start from this profile; the last successful run — yours or that agent’s — updates it.",
    );
  });
  it("9 · per-input", () => {
    expect(codeCaption()).toBe(
      "One profile per value of this input — each updated by its own runs.",
    );
  });
  it("10 · rotating credentials + one picked profile — steer", () => {
    expect(rotationWarning(2)).toBe(
      "This agent rotates 2 credentials but saves every run into one profile — accounts will overwrite each other’s sessions. Use per-input {{ credentials }} for one profile per account.",
    );
  });
  it("F5 · legacy persist-ON virtual pick label", () => {
    expect(virtualOwnProfileLabel("Checkout Bot")).toBe(
      "Checkout Bot’s profile",
    );
  });
  it("F6 · credential-pinned IP caption", () => {
    expect(credentialPinnedIpCaption("Kronos 5/15/26")).toBe(
      "IP pinned by credential “Kronos 5/15/26”",
    );
  });
  it("F7 · credential-attach warning (singular/plural)", () => {
    expect(credentialAttachWarning(1)).toBe(
      "1 agent picks this profile — attaching makes this credential its only writer; their runs will stop saving into it.",
    );
    expect(credentialAttachWarning(3)).toContain("3 agents pick this profile");
  });
});

describe("roleBadgeTooltip reuses the page tooltip copy", () => {
  it("maps each control role to the profiles-page tooltip", () => {
    expect(roleBadgeTooltip("workflow")).toContain("agent");
    expect(roleBadgeTooltip("credential")).toContain("credential");
    expect(roleBadgeTooltip("plain")).toContain("you");
  });
});

describe("roleSubtitle (dropdown-row derived line)", () => {
  it("credential rows say who keeps them signed in", () => {
    expect(
      roleSubtitle({
        browser_profile_id: "bp_1",
        is_managed: false,
        linked_credential_name: "Acme",
      }),
    ).toContain("Kept signed in by the credential");
  });
  it("agent (managed) rows say the agent updates them", () => {
    expect(
      roleSubtitle({ browser_profile_id: "bp_1", is_managed: true }),
    ).toContain("Updated by that agent");
  });
  it("plain rows lead with the id and 'yours'", () => {
    const sub = roleSubtitle({ browser_profile_id: "bp_12345678" });
    expect(sub).toContain("bp_1234");
    expect(sub).toContain("yours");
  });
});
