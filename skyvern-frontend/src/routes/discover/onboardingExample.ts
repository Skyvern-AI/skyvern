import { RunEngine } from "@/api/types";
import type { WorkflowCreateYAMLRequest } from "@/routes/workflows/types/workflowYamlTypes";

export const onboardingExamplePresentation = {
  title: "What a finished run looks like",
  provenance: "Example data, not your run",
  structure: [
    {
      title: "Visit a public page",
      detail: "Open Skyvern's public homepage without signing in.",
    },
    {
      title: "Read public content",
      detail: "Extract the headline and a short product summary.",
    },
    {
      title: "Return structured data",
      detail: "Keep the result to two short text fields.",
    },
  ],
  playback: [
    "Opened https://www.skyvern.com/ in this static agent example.",
    "The agent read the public headline and product description.",
    "Prepared the bounded agent result shown below.",
  ],
  result: {
    title: "Example result",
    fields: [
      {
        label: "Headline",
        value: "Automate browser-based work with AI",
      },
      {
        label: "Product summary",
        value:
          "Skyvern runs browser agents that interact with websites and extract structured data.",
      },
    ],
  },
} as const;

export const onboardingExampleRequest = {
  title: "Copyable agent example: Skyvern homepage summary",
  description:
    "Create an agent draft that reads public content from Skyvern's homepage.",
  workflow_definition: {
    parameters: [],
    blocks: [
      {
        block_type: "task",
        label: "Read Skyvern homepage",
        url: "https://www.skyvern.com/",
        navigation_goal: null,
        data_extraction_goal:
          "Extract the public headline and a short summary of what Skyvern does.",
        data_schema: {
          type: "object",
          properties: {
            headline: { type: "string", maxLength: 160 },
            product_summary: { type: "string", maxLength: 320 },
          },
          required: ["headline", "product_summary"],
          additionalProperties: false,
        },
        error_code_mapping: null,
        disable_cache: false,
        complete_criterion: null,
        terminate_criterion: null,
        include_action_history_in_verification: false,
        engine: RunEngine.SkyvernV1,
      },
    ],
  },
} satisfies WorkflowCreateYAMLRequest;
