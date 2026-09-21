import posthog, { type Properties } from "posthog-js";

type Variant = "legacy" | "revamp";

function capture(event: string, properties: Properties = {}): void {
  try {
    posthog.capture(`home.${event}`, properties);
  } catch {
    // PostHog may be unavailable in tests or before init.
  }
}

export const HomeTelemetry = {
  viewed: (variant: Variant) => capture("viewed", { variant }),
  promptSubmitted: (input: {
    source: "typed" | "example";
    example?: string;
    promptLength: number;
    handoff: boolean;
  }) => capture("prompt_submitted", input),
  exampleClicked: (input: { capability?: string; label: string }) =>
    capture("example_clicked", input),
  examplePreviewShown: (input: { capability: string; label: string }) =>
    capture("example_preview_shown", input),
  howItWorksToggled: (open: boolean) =>
    capture("how_it_works_toggled", { open }),
  addMenuOpened: () => capture("add_menu_opened"),
  uploadDocumentSelected: () => capture("upload_document_selected"),
  uploadDocumentFinished: (ok: boolean) =>
    capture("upload_document_finished", { ok }),
  recordTaskSelected: () => capture("record_task_selected"),
  voiceToggled: () => capture("voice_toggled"),
  // Legacy home only
  skipToBlankCanvasClicked: () => capture("skip_blank_canvas_clicked"),
  templateClicked: (input: { workflowPermanentId: string; title: string }) =>
    capture("template_clicked", input),
  advancedSettingsToggled: (open: boolean) =>
    capture("advanced_settings_toggled", { open }),
  improvePromptUsed: () => capture("improve_prompt_used"),
} as const;
