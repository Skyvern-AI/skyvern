import { describe, expect, it } from "vitest";

import {
  applyNarrativeEvent,
  EMPTY_NARRATIVE,
  hydrateNarrativeFromPayload,
  parseCredentialPause,
  parseCredentialPrompt,
} from "./narrativeState";
import type {
  WorkflowCopilotStreamErrorUpdate,
  WorkflowCopilotStreamResponseUpdate,
} from "./workflowCopilotTypes";

const basePayload = (
  overrides: Record<string, unknown> = {},
): Record<string, unknown> => ({
  turnId: "turn-1",
  turnIndex: 0,
  mode: "build",
  responseType: "REPLY",
  terminal: "response",
  startedAt: "2026-07-13T00:00:00Z",
  endedAt: "2026-07-13T00:00:05Z",
  ...overrides,
});

describe("parseCredentialPrompt", () => {
  it("keeps any non-empty reason string (card tolerates unknown tokens)", () => {
    expect(parseCredentialPrompt({ reason: "raw_secret" })).toEqual({
      reason: "raw_secret",
    });
    expect(parseCredentialPrompt({ reason: "a_future_reason" })).toEqual({
      reason: "a_future_reason",
    });
  });

  it("rejects malformed shapes", () => {
    expect(parseCredentialPrompt(null)).toBeNull();
    expect(parseCredentialPrompt("raw_secret")).toBeNull();
    expect(parseCredentialPrompt({})).toBeNull();
    expect(parseCredentialPrompt({ reason: "" })).toBeNull();
    expect(parseCredentialPrompt({ reason: 3 })).toBeNull();
  });
});

describe("parseCredentialPause", () => {
  it("accepts the four real outcomes and defaults credentialId to null", () => {
    expect(
      parseCredentialPause({ outcome: "connected", credentialId: "c1" }),
    ).toEqual({ outcome: "connected", credentialId: "c1" });
    expect(parseCredentialPause({ outcome: "skipped" })).toEqual({
      outcome: "skipped",
      credentialId: null,
    });
    expect(parseCredentialPause({ outcome: "timeout" })).toEqual({
      outcome: "timeout",
      credentialId: null,
    });
    expect(parseCredentialPause({ outcome: "declined" })).toEqual({
      outcome: "declined",
      credentialId: null,
    });
  });

  it("rejects unknown or malformed outcomes so the renderer can't crash", () => {
    expect(parseCredentialPause(null)).toBeNull();
    expect(parseCredentialPause({ outcome: "exploded" })).toBeNull();
    expect(parseCredentialPause({})).toBeNull();
  });
});

describe("hydrateNarrativeFromPayload — credential signals", () => {
  it("defaults both signals to null when absent", () => {
    expect(EMPTY_NARRATIVE.credentialPrompt).toBeNull();
    expect(EMPTY_NARRATIVE.credentialPause).toBeNull();
    const hydrated = hydrateNarrativeFromPayload(basePayload());
    expect(hydrated?.credentialPrompt).toBeNull();
    expect(hydrated?.credentialPause).toBeNull();
  });

  it("hydrates credentialPrompt from the narrative payload", () => {
    const hydrated = hydrateNarrativeFromPayload(
      basePayload({
        credentialPrompt: { reason: "credential_name_unresolved" },
      }),
    );
    expect(hydrated?.credentialPrompt).toEqual({
      reason: "credential_name_unresolved",
    });
  });

  it("hydrates a resolved credentialPause", () => {
    const hydrated = hydrateNarrativeFromPayload(
      basePayload({
        credentialPause: { outcome: "connected", credentialId: "cred-9" },
      }),
    );
    expect(hydrated?.credentialPause).toEqual({
      outcome: "connected",
      credentialId: "cred-9",
    });
  });

  it("carries both signals through the terminal response reducer (reload path)", () => {
    const response: WorkflowCopilotStreamResponseUpdate = {
      type: "response",
      workflow_copilot_chat_id: "chat-1",
      message: "Connect a credential to continue.",
      updated_workflow: null,
      response_time: "2026-07-13T00:00:05Z",
      proposal_disposition: "no_proposal",
      turn_id: "turn-1",
      narrative_payload: basePayload({
        credentialPrompt: { reason: "workflow_credential_inputs_unbound" },
        credentialPause: { outcome: "timeout", credentialId: null },
      }),
    } as WorkflowCopilotStreamResponseUpdate;
    const next = applyNarrativeEvent(EMPTY_NARRATIVE, response);
    expect(next.credentialPrompt).toEqual({
      reason: "workflow_credential_inputs_unbound",
    });
    expect(next.credentialPause).toEqual({
      outcome: "timeout",
      credentialId: null,
    });
  });

  it("leaves the signals null on a terminal error (no credential payload)", () => {
    const error: WorkflowCopilotStreamErrorUpdate = {
      type: "error",
      error: "Something went wrong.",
      turn_id: "turn-1",
    };
    const live = hydrateNarrativeFromPayload(basePayload({ terminal: null }))!;
    const next = applyNarrativeEvent(live, error);
    expect(next.credentialPrompt).toBeNull();
    expect(next.credentialPause).toBeNull();
  });
});
