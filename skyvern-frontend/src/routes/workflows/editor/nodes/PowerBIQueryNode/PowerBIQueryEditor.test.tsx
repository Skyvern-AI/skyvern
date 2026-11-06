import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { PageSlotsProvider } from "@/store/PageSlots";
import { PowerBIQueryEditor } from "./PowerBIQueryEditor";

const mocks = vi.hoisted(() => ({ datasetId: "", update: vi.fn() }));
vi.mock("@xyflow/react", () => ({
  useEdges: () => [],
  useNodes: () => [],
  useNodesData: () => ({
    type: "powerBIQuery",
    data: {
      editable: true,
      credentialSource: "nango",
      connectionId: "nc_first",
      datasetId: mocks.datasetId,
      query: "",
      maxRows: 1000,
      parameterKeys: [],
    },
  }),
}));
vi.mock("../../useUpdate", () => ({ useUpdate: () => mocks.update }));
vi.mock("../../workflowEditorUtils", () => ({
  getAvailableOutputParameterKeys: () => [],
}));
vi.mock("@/routes/workflows/editor/WorkflowScopeContext", () => ({
  useWorkflowScopeReadOnly: () => false,
}));
vi.mock("@/routes/workflows/components/CredentialSourceSelector", () => ({
  CredentialSourceSelector: () => null,
}));
vi.mock("@/components/HelpTooltip", () => ({ HelpTooltip: () => null }));
vi.mock("../TaskNode/ParametersMultiSelect", () => ({
  ParametersMultiSelect: () => null,
}));
vi.mock("@/components/WorkflowBlockInputTextarea", () => ({
  WorkflowBlockInputTextarea: (props: {
    "aria-label"?: string;
    value: string;
    onChange: (value: string) => void;
  }) => (
    <textarea
      aria-label={props["aria-label"]}
      value={props.value}
      onChange={(event) => props.onChange(event.target.value)}
    />
  ),
}));

afterEach(() => {
  cleanup();
  mocks.update.mockReset();
  mocks.datasetId = "";
});

it("keeps runtime dataset parameters editable alongside the picker", () => {
  mocks.datasetId = "{{ dataset_id }}";
  render(
    <PageSlotsProvider
      value={{
        powerBIDatasetPicker: ({ connectionId, onChange }) => (
          <button
            onClick={() => onChange("11111111-1111-4111-8111-111111111111")}
          >
            {connectionId}
          </button>
        ),
      }}
    >
      <PowerBIQueryEditor blockId="block_1" />
    </PageSlotsProvider>,
  );
  const parameter = screen.getByLabelText("Dataset workflow parameter");
  expect((parameter as HTMLTextAreaElement).value).toBe("{{ dataset_id }}");
  fireEvent.change(parameter, { target: { value: "{{ other_dataset }}" } });
  expect(mocks.update).toHaveBeenCalledWith({
    datasetId: "{{ other_dataset }}",
  });
  fireEvent.click(screen.getByRole("button", { name: "nc_first" }));
  expect(mocks.update).toHaveBeenCalledWith({
    datasetId: "11111111-1111-4111-8111-111111111111",
  });
});
