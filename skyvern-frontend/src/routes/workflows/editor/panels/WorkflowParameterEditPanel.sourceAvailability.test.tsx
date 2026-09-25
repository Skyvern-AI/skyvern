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

import { ReactFlowProvider } from "@xyflow/react";
import { stringify as toYaml } from "yaml";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { StudioWorkflowPanels } from "../../studio/StudioWorkflowPanels";
import { EditorPaneModeToggle } from "../../studio/EditorPaneHeader";
import CloudContext from "@/store/CloudContext";

const credentialsQuery = vi.hoisted(() => ({ isSuccess: false }));

vi.mock("@/routes/workflows/hooks/useCredentialsQuery", () => ({
  useCredentialsQuery: () => credentialsQuery,
}));
vi.mock("@/hooks/useCustomCredentialServiceConfig", () => ({
  useCustomCredentialServiceConfig: () => ({ parsedConfig: null }),
}));
vi.mock("../../hooks/useOnePasswordItemsQuery", () => ({
  useOnePasswordItemsQuery: () => ({ data: undefined, isError: false }),
}));
vi.mock("../../components/CredentialParameterSourceSelector", () => ({
  CredentialParameterSourceSelector: () => <div>Vault credential list</div>,
}));
vi.mock("../../components/BitwardenItemSelector", () => ({
  BitwardenItemSelector: () => <div>Bitwarden item list</div>,
}));

import { WorkflowParameterEditPanel } from "./WorkflowParameterEditPanel";

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeEach(() => {
  credentialsQuery.isSuccess = false;
  useWorkflowYamlEditorStore.setState(
    useWorkflowYamlEditorStore.getInitialState(),
  );
  useWorkflowParametersStore.setState(
    useWorkflowParametersStore.getInitialState(),
  );
  useWorkflowPanelStore.setState(useWorkflowPanelStore.getInitialState());
  vi.stubGlobal("ResizeObserver", ResizeObserverStub);
  window.HTMLElement.prototype.scrollIntoView = () => {};
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  useWorkflowYamlEditorStore.setState(
    useWorkflowYamlEditorStore.getInitialState(),
  );
});

function panel(isCloud: boolean) {
  return (
    <CloudContext.Provider value={isCloud}>
      <WorkflowParameterEditPanel
        type="workflow"
        onClose={vi.fn()}
        onSave={vi.fn()}
      />
    </CloudContext.Provider>
  );
}

function enterCredentialMode() {
  const valueTypeSelect = screen.getAllByRole("combobox")[0];
  if (!valueTypeSelect) throw new Error("Value Type select not found");
  fireEvent.click(valueTypeSelect);
  fireEvent.click(screen.getByRole("option", { name: "credential" }));
}

function openSourceSelect() {
  const sourceSelect = screen.getAllByRole("combobox")[2];
  if (!sourceSelect) throw new Error("Source select not found");
  fireEvent.click(sourceSelect);
}

function renderPanel(isCloud: boolean) {
  render(panel(isCloud));

  enterCredentialMode();
  openSourceSelect();
}

describe("WorkflowParameterEditPanel Skyvern source availability", () => {
  it("shows Skyvern in OSS when the credentials query succeeds", () => {
    credentialsQuery.isSuccess = true;
    renderPanel(false);

    expect(screen.getByRole("option", { name: "Skyvern" })).toBeTruthy();
  });

  it("hides Skyvern in OSS when the credentials query errors", () => {
    credentialsQuery.isSuccess = false;
    renderPanel(false);

    expect(screen.queryByRole("option", { name: "Skyvern" })).toBeNull();
  });

  it("shows Skyvern in cloud regardless of the credentials query", () => {
    credentialsQuery.isSuccess = false;
    renderPanel(true);

    expect(screen.getByRole("option", { name: "Skyvern" })).toBeTruthy();
  });

  it("updates the untouched OSS default when the capability probe succeeds", async () => {
    const view = render(panel(false));
    enterCredentialMode();
    expect(screen.getAllByRole("combobox")[2]?.textContent).toContain(
      "Bitwarden",
    );

    credentialsQuery.isSuccess = true;
    view.rerender(panel(false));

    await waitFor(() =>
      expect(screen.getAllByRole("combobox")[2]?.textContent).toContain(
        "Skyvern",
      ),
    );
  });
});

describe("Studio Inputs with an open YAML draft", () => {
  it.each(["add", "delete", "default"])(
    "keeps a panel %s when switching a stale draft to Visual",
    async (operation) => {
      const original = {
        key: "query",
        parameterType: "workflow" as const,
        dataType: "string" as const,
        defaultValue: "original",
        description: "",
      };
      const originalParameters = operation === "add" ? [] : [original];
      useWorkflowParametersStore.getState().setParameters(originalParameters);
      const yaml = useWorkflowYamlEditorStore.getState();
      const draft = toYaml({
        title: "Original",
        workflow_definition: { parameters: originalParameters, blocks: [] },
      });
      yaml.registerEnterYamlMode(() => yaml.open(draft));
      const commit = vi.fn(async () => {
        useWorkflowParametersStore
          .getState()
          .setParameters(originalParameters, { fromYamlCommit: true });
        yaml.close();
        return true;
      });
      yaml.registerCommit(commit);
      yaml.open(draft);
      yaml.setDraft(draft.replace("Original", "YAML title"));
      useWorkflowPanelStore
        .getState()
        .setWorkflowPanelState({ active: true, content: "parameters" });
      const view = render(
        <ReactFlowProvider>
          <TooltipProvider>
            <StudioWorkflowPanels />
            <EditorPaneModeToggle />
          </TooltipProvider>
        </ReactFlowProvider>,
      );
      if (operation === "delete") {
        fireEvent.click(view.container.querySelector("section button")!);
      } else {
        if (operation === "add")
          fireEvent.click(screen.getByRole("button", { name: "Add Input" }));
        else
          fireEvent.click(
            view.container.querySelector("section svg.cursor-pointer")!,
          );
        if (operation === "add")
          fireEvent.change(screen.getAllByRole("textbox")[0]!, {
            target: { value: "added_input" },
          });
        else
          fireEvent.change(screen.getByDisplayValue("original"), {
            target: { value: "changed" },
          });
        fireEvent.click(screen.getByRole("button", { name: "Save" }));
      }
      const changed = structuredClone(
        useWorkflowParametersStore.getState().parameters,
      );
      if (operation === "delete") expect(changed).toEqual([]);
      else
        expect(changed).toEqual([
          expect.objectContaining(
            operation === "add"
              ? { key: "added_input" }
              : { key: "query", defaultValue: "changed" },
          ),
        ]);
      await act(async () =>
        fireEvent.click(screen.getByRole("button", { name: "Visual" })),
      );
      expect(useWorkflowParametersStore.getState().parameters).toEqual(changed);
      expect(commit).not.toHaveBeenCalled();
      expect(useWorkflowYamlEditorStore.getState()).toMatchObject({
        active: true,
        stale: true,
        draft: draft.replace("Original", "YAML title"),
      });
    },
  );
});
