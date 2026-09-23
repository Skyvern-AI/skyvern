import posthog, { type Properties } from "posthog-js";
import { nanoid } from "nanoid";

type Variant = "legacy" | "revamp";
type AgentCreationSource = "typed" | "example" | "blank";

type AgentCreationAttempt = {
  attemptId: string;
  source: AgentCreationSource;
  handoff: boolean;
  variant: Variant;
  example?: string;
  exampleEdited?: boolean;
};

function capture(event: string, properties: Properties = {}): void {
  try {
    posthog.capture(`home.${event}`, properties);
  } catch {
    // PostHog may be unavailable in tests or before init.
  }
}

function attemptProperties(attempt: AgentCreationAttempt): Properties {
  return {
    attempt_id: attempt.attemptId,
    source: attempt.source,
    handoff: attempt.handoff,
    variant: attempt.variant,
    ...(attempt.example ? { example: attempt.example } : {}),
    ...(attempt.exampleEdited !== undefined
      ? { example_edited: attempt.exampleEdited }
      : {}),
  };
}

function generateAttemptId(): string {
  try {
    return crypto.randomUUID();
  } catch {
    return nanoid();
  }
}

function errorCategory(error: unknown): string {
  if (typeof error !== "object" || error === null) return "unknown";

  const candidate = error as {
    isAxiosError?: unknown;
    response?: { status?: unknown };
  };
  const status = candidate.response?.status;
  if (typeof status === "number") {
    if (status === 400 || status === 422) return "invalid_request";
    if (status === 401) return "authentication_required";
    if (status === 402) return "payment_required";
    if (status === 403) return "forbidden";
    if (status === 404) return "not_found";
    if (status === 409) return "conflict";
    if (status === 429) return "rate_limited";
    if (status >= 500) return "server_error";
    return "http_error";
  }
  if (candidate.isAxiosError === true) return "network_error";
  return "client_error";
}

export const HomeTelemetry = {
  // Canonical home exposure. The caller emits once per mounted page exposure,
  // after resolving a mutually exclusive legacy/revamp variant.
  viewed: (variant: Variant) => capture("viewed", { variant }),
  promptSubmitted: (input: {
    attemptId: string;
    source: "typed" | "example";
    example?: string;
    exampleEdited?: boolean;
    promptLength: number;
    handoff: boolean;
  }) => {
    const { attemptId, exampleEdited, ...properties } = input;
    capture("prompt_submitted", {
      attempt_id: attemptId,
      ...properties,
      ...(exampleEdited !== undefined ? { example_edited: exampleEdited } : {}),
    });
  },
  agentCreationSubmitted: (
    input: Omit<AgentCreationAttempt, "attemptId">,
  ): AgentCreationAttempt => {
    const attempt = { ...input, attemptId: generateAttemptId() };
    capture("agent_creation_submitted", attemptProperties(attempt));
    return attempt;
  },
  agentCreationSucceeded: (
    attempt: AgentCreationAttempt,
    workflowPermanentId: string,
  ) =>
    capture("agent_creation_succeeded", {
      ...attemptProperties(attempt),
      workflow_permanent_id: workflowPermanentId,
    }),
  agentCreationFailed: (attempt: AgentCreationAttempt, error: unknown) =>
    capture("agent_creation_failed", {
      ...attemptProperties(attempt),
      error_category: errorCategory(error),
    }),
  exampleClicked: (input: {
    example?: string;
    capability?: string;
    label: string;
  }) => capture("example_clicked", input),
  examplePreviewShown: (input: {
    example: string;
    capability: string;
    label: string;
  }) => capture("example_preview_shown", input),
  howItWorksToggled: (open: boolean) =>
    capture("how_it_works_toggled", { open }),
  addMenuOpened: () => capture("add_menu_opened"),
  uploadDocumentSelected: () => capture("upload_document_selected"),
  uploadDocumentFinished: (ok: boolean) =>
    capture("upload_document_finished", { ok }),
  recordTaskSelected: () => capture("record_task_selected"),
  voiceToggled: () => capture("voice_toggled"),
  skipToBlankCanvasClicked: (attempt: AgentCreationAttempt) =>
    capture("skip_blank_canvas_clicked", attemptProperties(attempt)),
  templateClicked: (input: { workflowPermanentId: string; title: string }) =>
    capture("template_clicked", input),
  advancedSettingsToggled: (open: boolean) =>
    capture("advanced_settings_toggled", { open }),
  improvePromptUsed: () => capture("improve_prompt_used"),
} as const;

export type { AgentCreationAttempt };
