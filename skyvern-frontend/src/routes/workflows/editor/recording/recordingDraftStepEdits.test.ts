import { describe, expect, it } from "vitest";

import type { RecordingDraftStep } from "@/store/useRecordingStore";

import {
  buildDraftStepTitlePatch,
  deriveUrlFromNavigationTitle,
} from "./recordingDraftStepEdits";

const navigationStep: RecordingDraftStep = {
  step_id: "bs-recording-step-0",
  action_kind: "url_change",
  block_type: "goto_url",
  label: "goto_wikipedia_com",
  url: "https://www.wikipedia.com/wiki/Foo",
  status: "ready",
  editable_fields: ["label", "url"],
  parameters: [],
  parameter_keys: [],
};

describe("deriveUrlFromNavigationTitle", () => {
  it("updates hostname while preserving path from the original URL", () => {
    expect(
      deriveUrlFromNavigationTitle(navigationStep.url, "Go to wikipedia.org"),
    ).toBe("https://wikipedia.org/wiki/Foo");
  });

  it("accepts a full URL in the title", () => {
    expect(
      deriveUrlFromNavigationTitle(
        navigationStep.url,
        "Go to https://example.org/start",
      ),
    ).toBe("https://example.org/start");
  });
});

describe("buildDraftStepTitlePatch", () => {
  it("patches url and label for navigation steps", () => {
    expect(
      buildDraftStepTitlePatch(navigationStep, "Go to wikipedia.org"),
    ).toEqual({
      title: "Go to wikipedia.org",
      url: "https://wikipedia.org/wiki/Foo",
      label: "Go_to_wikipedia_org",
    });
  });

  it("patches title only for action steps", () => {
    const actionStep: RecordingDraftStep = {
      ...navigationStep,
      action_kind: "click",
      block_type: "action",
      title: "Click 'Search'",
    };

    expect(
      buildDraftStepTitlePatch(actionStep, "Click the search button"),
    ).toEqual({
      title: "Click the search button",
    });
  });
});
