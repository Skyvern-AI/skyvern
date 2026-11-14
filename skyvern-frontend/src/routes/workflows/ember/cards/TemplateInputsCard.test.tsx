// @vitest-environment jsdom
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import {
  AUTHORING_SAVE_MESSAGE,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";
import type { ParametersState } from "../../editor/types";
import { TemplateInputsCard } from "./TemplateInputsCard";

const onSave = vi.hoisted(() => vi.fn());
vi.mock("../../editor/hooks/useSaveWorkflow", () => ({
  useSaveWorkflow: () => onSave,
}));
vi.mock("../../components/CredentialCombobox", () => ({
  CredentialCombobox: ({
    onValueChange,
  }: {
    onValueChange: (value: string) => void;
  }) => (
    <button onClick={() => onValueChange("cred_new")}>pick credential</button>
  ),
}));
vi.mock("@/routes/credentials/CredentialsModal", () => ({
  CredentialsModal: () => null,
}));

function seed(parameters: ParametersState) {
  useWorkflowParametersStore.setState({ parameters });
}

const saveButton = () =>
  screen.getByRole("button", { name: "Save inputs" }) as HTMLButtonElement;

describe("TemplateInputsCard", () => {
  beforeEach(() => {
    onSave.mockReset().mockResolvedValue(undefined);
  });
  afterEach(() => {
    cleanup();
    useWorkflowHasChangesStore.setState(
      useWorkflowHasChangesStore.getInitialState(),
    );
    useWorkflowYamlEditorStore.setState({ authoringInProgress: false });
  });

  it("blocks an invalid number and saves a cleared number as null", async () => {
    seed([
      {
        key: "retries",
        parameterType: "workflow",
        dataType: "integer",
        defaultValue: 3,
      },
    ]);
    render(<TemplateInputsCard />);
    const field = screen.getByLabelText("retries");

    fireEvent.change(field, { target: { value: "abc" } });
    expect(saveButton().disabled).toBe(true);

    fireEvent.change(field, { target: { value: "" } });
    expect(saveButton().disabled).toBe(false);
    fireEvent.click(saveButton());

    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(useWorkflowParametersStore.getState().parameters[0]).toMatchObject({
      defaultValue: null,
    });
    expect(await screen.findByText("Saved")).toBeTruthy();
  });

  it("clears the rotation pool when a credential is chosen", async () => {
    seed([
      {
        key: "login",
        parameterType: "credential",
        credentialId: "cred_old",
        credentialIds: ["cred_old", "cred_other"],
        selectionStrategy: "round_robin",
        fallbackCredentialIds: ["cred_other"],
      } as ParametersState[number],
    ]);
    render(<TemplateInputsCard />);

    fireEvent.click(screen.getByText("pick credential"));
    fireEvent.click(saveButton());

    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(useWorkflowParametersStore.getState().parameters[0]).toMatchObject({
      credentialId: "cred_new",
      credentialIds: null,
      selectionStrategy: null,
      fallbackCredentialIds: null,
    });
  });

  it("shows Not saved when the save fails", async () => {
    onSave.mockRejectedValue(new Error("save failed"));
    seed([
      {
        key: "url",
        parameterType: "workflow",
        dataType: "string",
        defaultValue: "",
      },
    ]);
    render(<TemplateInputsCard />);

    fireEvent.change(screen.getByLabelText("url"), {
      target: { value: "https://example.com" },
    });
    fireEvent.click(saveButton());

    expect(await screen.findByText("Not saved")).toBeTruthy();
  });

  it.each([
    {
      hold: "a save hold",
      reason: "Copilot is saving your accepted changes.",
      set: (on: boolean) =>
        useWorkflowHasChangesStore
          .getState()
          .setSaveBlockedReason(
            on ? "Copilot is saving your accepted changes." : null,
          ),
    },
    {
      hold: "an authoring action",
      reason: AUTHORING_SAVE_MESSAGE,
      set: (on: boolean) =>
        useWorkflowYamlEditorStore.setState({ authoringInProgress: on }),
    },
  ])(
    "holds Save inputs under $hold and keeps its reason reachable",
    ({ reason, set }) => {
      set(true);
      seed([
        {
          key: "url",
          parameterType: "workflow",
          dataType: "string",
          defaultValue: "",
        },
      ]);
      render(<TemplateInputsCard />);
      fireEvent.change(screen.getByLabelText("url"), {
        target: { value: "https://example.com" },
      });

      // The name stays the action; the reason is the description, not part of the name.
      const held = screen.getByRole("button", {
        name: "Save inputs (paused)",
        description: reason,
      }) as HTMLButtonElement;
      expect(held.disabled).toBe(true);
      // A disabled button swallows its own tooltip trigger's events.
      const wrapper = held.closest<HTMLElement>("[tabindex='0']");
      expect(wrapper?.getAttribute("aria-describedby")).toBe(
        held.getAttribute("aria-describedby"),
      );

      act(() => set(false));
      expect(saveButton().disabled).toBe(false);
    },
  );
});
