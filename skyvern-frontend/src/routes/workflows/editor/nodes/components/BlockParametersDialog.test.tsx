// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { WorkflowParameter } from "@/routes/workflows/types/workflowTypes";
import { BlockParametersDialog } from "./BlockParametersDialog";

const category = {
  key: "category",
  parameter_type: "workflow",
  workflow_parameter_type: "string",
  description: null,
  default_value: null,
} as unknown as WorkflowParameter;

describe("BlockParametersDialog from the Doc", () => {
  it("runs on Enter, keeps Shift+Enter as a line break, and names the step in Doc words", () => {
    const onSubmit = vi.fn();
    render(
      <BlockParametersDialog
        open
        onOpenChange={() => {}}
        blockLabel="go_to_url_2"
        parameters={[category]}
        initialValues={{ category: "Travel" }}
        onSubmit={onSubmit}
        inDoc
      />,
    );
    expect(screen.getByText("Run this step with")).toBeTruthy();
    expect(screen.queryByText(/go_to_url_2|block/i)).toBeNull();
    const field = screen.getByRole("textbox");
    fireEvent.keyDown(field, { key: "Enter", shiftKey: true });
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.keyDown(field, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledWith({ category: "Travel" });
  });

  it("does not run when another control already handled the Enter", () => {
    const onSubmit = vi.fn();
    render(
      <BlockParametersDialog
        open
        onOpenChange={() => {}}
        blockLabel="go_to_url_2"
        parameters={[category]}
        initialValues={{ category: "Travel" }}
        onSubmit={onSubmit}
        inDoc
      />,
    );
    const field = screen.getByRole("textbox");
    field.addEventListener("keydown", (e) => e.preventDefault());
    fireEvent.keyDown(field, { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("does not run on the Enter that commits an IME candidate, whichever way the browser reports it", () => {
    const onSubmit = vi.fn();
    render(
      <BlockParametersDialog
        open
        onOpenChange={() => {}}
        blockLabel="go_to_url_2"
        parameters={[category]}
        initialValues={{ category: "Travel" }}
        onSubmit={onSubmit}
        inDoc
      />,
    );
    const field = screen.getByRole("textbox");
    fireEvent.keyDown(field, { key: "Enter", isComposing: true });
    // Safari ends composition first, so its committing Enter reports keyCode 229 and isComposing false.
    fireEvent.keyDown(field, { key: "Enter", keyCode: 229 });
    expect(onSubmit).not.toHaveBeenCalled();
  });
});
