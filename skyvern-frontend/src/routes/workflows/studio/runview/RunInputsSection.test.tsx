// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import type { WorkflowParameter } from "../../types/workflowTypes";
import { RunInputsSection } from "./RunInputsSection";

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

  test("renders parameters as labeled fields: primitive prose, nested tree, description", () => {
    render(
      <RunInputsSection
        {...emptyProps}
        parameters={[
          [
            "invoice_url",
            "https://example.test/invoice.pdf",
            {
              description: "Where the invoice lives",
            } as unknown as WorkflowParameter,
          ],
          ["line_items", { count: 3 }],
        ]}
      />,
    );

    expect(screen.queryByText("Run inputs")).not.toBeNull();
    expect(screen.queryByText("invoice_url")).not.toBeNull();
    expect(screen.queryByText("Where the invoice lives")).not.toBeNull();
    // Primitive renders as prose text, not a single JSON dump.
    expect(
      screen.queryByText("https://example.test/invoice.pdf"),
    ).not.toBeNull();
    // Nested value renders the collapsible searchable tree (JsonExplorer).
    expect(screen.queryByPlaceholderText("Search JSON")).not.toBeNull();
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

  test("toggles the clamped prose block via Show more / Show less", () => {
    // jsdom has no layout engine, so mock the clamp overflow the component
    // measures (scrollHeight > clientHeight).
    vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(200);
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(96);

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
                prompt: "A synthetic prompt long enough to overflow the clamp.",
              },
            ],
          },
        ]}
      />,
    );

    const toggle = screen.getByRole("button", { name: "Show more" });
    const promptText = "A synthetic prompt long enough to overflow the clamp.";
    // Collapsed: the prose block carries the line clamp.
    expect(screen.getByText(promptText).className).toContain("line-clamp-6");

    fireEvent.click(toggle);
    expect(screen.queryByRole("button", { name: "Show less" })).not.toBeNull();
    expect(screen.queryByRole("button", { name: "Show more" })).toBeNull();
    // Expanded: the clamp is dropped so the full prompt is visible.
    expect(screen.getByText(promptText).className).not.toContain(
      "line-clamp-6",
    );
  });

  test("renders the whole prompt as one wrapped block, not per-line rows", () => {
    // Prose keeps the entire prompt in the DOM and clamps height via CSS; the
    // old code inset sliced hidden lines out, so a collapsed multi-line prompt
    // must now expose every line (copy/accessibility) as a single text node.
    const prompt = Array.from({ length: 20 }, (_, i) => `line ${i + 1}`).join(
      "\n",
    );
    const { container } = render(
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

    expect(container.textContent).toContain("line 1");
    expect(container.textContent).toContain("line 20");
    // No per-line row elements: the prompt lives in one node, so no element's
    // text is exactly "line 6".
    expect(screen.queryByText("line 6")).toBeNull();
  });
});
