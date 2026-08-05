// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConfirmDialog } from "./confirm-dialog";

afterEach(cleanup);

function confirmButton() {
  return screen.getByRole("button", { name: "Delete" }) as HTMLButtonElement;
}

function cancelButton() {
  return screen.getByRole("button", { name: "Cancel" }) as HTMLButtonElement;
}

describe("ConfirmDialog", () => {
  it("renders the title, description, and standardized reversibility line", () => {
    render(
      <ConfirmDialog
        open
        onOpenChange={() => {}}
        title="Delete this credential?"
        description="The credential Prod API Key will be deleted."
        onConfirm={() => {}}
      />,
    );

    expect(screen.getByText("Delete this credential?")).toBeTruthy();
    expect(
      screen.getByText("The credential Prod API Key will be deleted."),
    ).toBeTruthy();
    expect(screen.getByText("This can't be undone.")).toBeTruthy();
  });

  it("omits the reversibility line when reversible", () => {
    render(
      <ConfirmDialog
        open
        onOpenChange={() => {}}
        title="Unassign folder?"
        description="The credentials in this folder will be unassigned, not deleted."
        reversible
        onConfirm={() => {}}
      />,
    );

    expect(screen.queryByText("This can't be undone.")).toBeNull();
  });

  it("lets a surface override the reversibility line with stronger wording", () => {
    render(
      <ConfirmDialog
        open
        onOpenChange={() => {}}
        title="Delete credential?"
        reversibilityNote="The Skyvern team has no way to restore a credential once it's deleted."
        onConfirm={() => {}}
      />,
    );

    expect(screen.queryByText("This can't be undone.")).toBeNull();
    expect(
      screen.getByText(
        "The Skyvern team has no way to restore a credential once it's deleted.",
      ),
    ).toBeTruthy();
  });

  it("confirms immediately when no typed confirmation is required", () => {
    const onConfirm = vi.fn();
    render(
      <ConfirmDialog
        open
        onOpenChange={() => {}}
        title="Delete schedule?"
        onConfirm={onConfirm}
      />,
    );

    expect(confirmButton().disabled).toBe(false);
    fireEvent.click(confirmButton());
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("requires typed confirmation for bulk deletes at or above the threshold", () => {
    const onConfirm = vi.fn();
    render(
      <ConfirmDialog
        open
        onOpenChange={() => {}}
        title="Delete 12 agents?"
        itemCount={12}
        onConfirm={onConfirm}
      />,
    );

    const input = screen.getByRole("textbox") as HTMLInputElement;
    expect(input).toBeTruthy();
    expect(confirmButton().disabled).toBe(true);

    fireEvent.click(confirmButton());
    expect(onConfirm).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { value: "  DELETE  " } });
    expect(confirmButton().disabled).toBe(false);

    fireEvent.click(confirmButton());
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("does not require typed confirmation below the threshold", () => {
    render(
      <ConfirmDialog
        open
        onOpenChange={() => {}}
        title="Delete 3 agents?"
        itemCount={3}
        onConfirm={() => {}}
      />,
    );

    expect(screen.queryByRole("textbox")).toBeNull();
    expect(confirmButton().disabled).toBe(false);
  });

  it("can force typed confirmation independent of item count", () => {
    render(
      <ConfirmDialog
        open
        onOpenChange={() => {}}
        title="Clear all scripts?"
        requireTypedConfirmation
        confirmationPhrase="clear"
        confirmLabel="Clear All"
        onConfirm={() => {}}
      />,
    );

    const input = screen.getByRole("textbox") as HTMLInputElement;
    const button = screen.getByRole("button", {
      name: "Clear All",
    }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);

    fireEvent.change(input, { target: { value: "clear" } });
    expect(button.disabled).toBe(false);
  });

  it("keeps confirm disabled while confirmDisabled is set (usage-lookup gating)", () => {
    render(
      <ConfirmDialog
        open
        onOpenChange={() => {}}
        title="Delete browser profile?"
        confirmDisabled
        onConfirm={() => {}}
      />,
    );

    expect(confirmButton().disabled).toBe(true);
  });

  it("disables both actions and shows a spinner while pending", () => {
    const onOpenChange = vi.fn();
    const { container } = render(
      <ConfirmDialog
        open
        onOpenChange={onOpenChange}
        title="Delete schedule?"
        isPending
        onConfirm={() => {}}
      />,
    );

    expect(confirmButton().disabled).toBe(true);
    expect(cancelButton().disabled).toBe(true);
    expect(container.ownerDocument.querySelector(".animate-spin")).toBeTruthy();
  });

  it("cancel requests a close", () => {
    const onOpenChange = vi.fn();
    render(
      <ConfirmDialog
        open
        onOpenChange={onOpenChange}
        title="Delete schedule?"
        onConfirm={() => {}}
      />,
    );

    fireEvent.click(cancelButton());
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});
