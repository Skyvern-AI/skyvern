// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { RunInputsSection } from "./RunInputsSection";

vi.mock("./OverviewCodeBlock", () => ({
  OverviewCodeBlock: ({ value }: { value: string }) => <pre>{value}</pre>,
}));

const emptyProps = {
  parameters: [],
  blockPrompts: [],
  meta: [],
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("RunInputsSection", () => {
  test("renders block labels and prompts under a Block prompts section", () => {
    render(
      <RunInputsSection
        {...emptyProps}
        blockPrompts={[
          {
            blockLabel: "summary block",
            blockType: "text_prompt",
            fields: [{ fieldLabel: "Prompt", prompt: "Summarize the input" }],
          },
        ]}
      />,
    );

    expect(screen.queryByText("Block prompts")).not.toBeNull();
    expect(screen.queryByText("summary block")).not.toBeNull();
    expect(screen.queryByText("Summarize the input")).not.toBeNull();
  });

  test("gates parameters-only, prompts-only, combined, and empty states", () => {
    const parametersOnly = render(
      <RunInputsSection
        {...emptyProps}
        parameters={[["synthetic_key", "synthetic value"]]}
      />,
    );
    expect(screen.queryByText("Run inputs")).not.toBeNull();
    expect(screen.queryByText(/synthetic_key/)).not.toBeNull();
    parametersOnly.unmount();

    const promptsOnly = render(
      <RunInputsSection
        {...emptyProps}
        blockPrompts={[
          {
            blockLabel: "prompt-only block",
            blockType: "text_prompt",
            fields: [{ fieldLabel: "Prompt", prompt: "Prompt-only value" }],
          },
        ]}
      />,
    );
    expect(screen.queryByText("Block prompts")).not.toBeNull();
    expect(screen.queryByText("Run inputs")).toBeNull();
    expect(screen.queryByText("Prompt-only value")).not.toBeNull();
    promptsOnly.unmount();

    const combined = render(
      <RunInputsSection
        {...emptyProps}
        parameters={[["combined_key", "combined value"]]}
        blockPrompts={[
          {
            blockLabel: "combined block",
            blockType: "text_prompt",
            fields: [{ fieldLabel: "Prompt", prompt: "Combined prompt" }],
          },
        ]}
      />,
    );
    const parameterJson = screen.getByText(/combined_key/);
    const combinedPrompt = screen.getByText("Combined prompt");
    expect(
      parameterJson.compareDocumentPosition(combinedPrompt) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).not.toBe(0);
    // Run inputs and Block prompts are distinct sibling sections.
    expect(screen.queryByText("Run inputs")).not.toBeNull();
    expect(screen.queryByText("Block prompts")).not.toBeNull();
    combined.unmount();

    const empty = render(<RunInputsSection {...emptyProps} />);
    expect(empty.container.firstChild).toBeNull();
  });

  test("labels every prompt inset, including single-field blocks", () => {
    render(
      <RunInputsSection
        {...emptyProps}
        blockPrompts={[
          {
            blockLabel: "single-field block",
            blockType: "text_prompt",
            fields: [{ fieldLabel: "Prompt", prompt: "Single prompt" }],
          },
          {
            blockLabel: "multi-field block",
            blockType: "task",
            fields: [
              { fieldLabel: "Navigation goal", prompt: "Navigate somewhere" },
              { fieldLabel: "Extraction goal", prompt: "Extract something" },
            ],
          },
        ]}
      />,
    );

    // The locked mock labels every inset, single-field blocks included.
    expect(screen.queryByText("Prompt")).not.toBeNull();
    expect(screen.queryByText("Navigation goal")).not.toBeNull();
    expect(screen.queryByText("Extraction goal")).not.toBeNull();
  });

  test("renders the expansion toggle when measured content overflows", () => {
    // jsdom has no layout engine, so it cannot measure line-clamp overflow itself.
    vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(80);
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(48);

    render(
      <RunInputsSection
        {...emptyProps}
        blockPrompts={[
          {
            blockLabel: "long prompt block",
            blockType: "text_prompt",
            fields: [
              {
                fieldLabel: "Prompt",
                prompt:
                  "A synthetic prompt long enough to overflow three lines.",
              },
            ],
          },
        ]}
      />,
    );

    expect(screen.queryByRole("button", { name: "Show more" })).not.toBeNull();
  });

  test("bounds the collapsed render and toggles many-line prompts without layout", () => {
    // 20 short lines: jsdom can't measure height, so the line-count cap alone
    // must drive the toggle and keep the hidden lines out of the DOM.
    const prompt = Array.from({ length: 20 }, (_, i) => `line ${i + 1}`).join(
      "\n",
    );
    render(
      <RunInputsSection
        {...emptyProps}
        blockPrompts={[
          {
            blockLabel: "long block",
            blockType: "text_prompt",
            fields: [{ fieldLabel: "Prompt", prompt }],
          },
        ]}
      />,
    );

    expect(screen.queryByText("line 6")).not.toBeNull();
    expect(screen.queryByText("line 7")).toBeNull();
    const toggle = screen.getByRole("button", { name: "Show more" });

    fireEvent.click(toggle);
    expect(screen.queryByText("line 20")).not.toBeNull();
  });
});
