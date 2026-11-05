// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { parse as parseYaml } from "yaml";
import * as axiosClient from "@/api/AxiosClient";
import { useCopilotProposalStore } from "@/store/CopilotProposalStore";
import {
  useWorkflowHasChangesStore,
  type WorkflowSaveData,
} from "@/store/WorkflowHasChangesStore";
import type {
  WorkflowApiResponse,
  WorkflowBlock,
} from "../../types/workflowTypes";
import { getElements, getWorkflowBlocks } from "../workflowEditorUtils";
import { settings } from "./docTestBlocks";
import { DocProposalCard, useDocProposal } from "./DocProposalView";

const post = vi.fn();
const decide = vi.fn(async () => true);
const input = {
  parameter_type: "workflow",
  key: "account_id",
  workflow_parameter_type: "string",
  default_value: null,
  workflow_parameter_id: "wp_1",
};
const step = (extra: Record<string, unknown> = {}) =>
  ({
    label: "read_input",
    block_type: "navigation",
    navigation_goal: "Open {{ account_id }}",
    next_block_label: null,
    parameters: [input],
    output_parameter: { parameter_type: "output", key: "read_input_output" },
    ...extra,
  }) as unknown as WorkflowBlock;
const workflow = (block: WorkflowBlock) =>
  ({
    workflow_id: "w_1",
    workflow_permanent_id: "wpid_1",
    title: "Synthetic",
    description: null,
    run_with: "agent",
    code_version: null,
    workflow_definition: { version: 2, blocks: [block], parameters: [input] },
  }) as unknown as WorkflowApiResponse;

beforeEach(() => {
  useWorkflowHasChangesStore.setState(
    useWorkflowHasChangesStore.getInitialState(),
  );
  post.mockReset();
  decide.mockClear();
  vi.spyOn(axiosClient, "getClient").mockResolvedValue({ post } as never);
});
afterEach(() => {
  cleanup();
  useCopilotProposalStore.getState().setProposal(null);
  vi.restoreAllMocks();
});

async function renderProposal(block = step()) {
  const proposed = workflow(block);
  const live = getElements([block], settings(), true);
  const blocks = getWorkflowBlocks(live.nodes, live.edges);
  if (blocks[0]?.block_type === "task_v2") blocks[0].max_steps = null;
  useWorkflowHasChangesStore.setState({
    getSaveData: () =>
      ({
        workflow: proposed,
        title: proposed.title,
        description: null,
        blocks,
        parameters: [input],
        workflowDefinitionVersion: 2,
        settings: { proxyLocation: "RESIDENTIAL", runWith: "agent" },
      }) as unknown as WorkflowSaveData,
  });
  useCopilotProposalStore.getState().setProposal({
    proposed,
    baseline: null,
    review: null,
    actionable: true,
    acceptable: true,
    decide,
  });
  function View() {
    const proposal = useDocProposal("wpid_1", live.nodes, live.edges);
    return proposal ? (
      <DocProposalCard proposal={proposal} goTo={() => {}} />
    ) : null;
  }
  await act(async () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <View />
      </QueryClientProvider>,
    );
  });
}

it("converts an unstaged reload baseline's parameter keys before classifying changes", async () => {
  post.mockResolvedValue({
    data: { workflow_definition: workflow(step()).workflow_definition },
  });
  await renderProposal();
  expect(post).toHaveBeenCalledOnce();
  expect(post.mock.calls[0]![0]).toBe(
    "/workflow/copilot/convert-yaml-to-blocks",
  );
  expect(
    parseYaml(post.mock.calls[0]![1].workflow_definition_yaml).blocks[0]
      .parameter_keys,
  ).toEqual(["account_id"]);
  expect(
    screen.getByRole("region", { name: "Copilot proposal" }).textContent,
  ).not.toContain("step changed");
});

it("uses the converter's deployment default of 40, not a guessed max_steps value", async () => {
  const block = step({
    block_type: "task_v2",
    prompt: "Read the input",
    max_steps: 40,
    max_iterations: 50,
  });
  post.mockResolvedValue({
    data: { workflow_definition: workflow(block).workflow_definition },
  });
  await renderProposal(block);
  expect(post).toHaveBeenCalledOnce();
  expect(
    parseYaml(post.mock.calls[0]![1].workflow_definition_yaml).blocks[0]
      .max_steps,
  ).toBeNull();
  expect(
    screen.getByRole("region", { name: "Copilot proposal" }).textContent,
  ).not.toContain("step changed");
});

it("gates only Doc Accept while conversion is pending", async () => {
  let resolve!: (value: unknown) => void;
  post.mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  await renderProposal();
  expect(screen.getByRole("status").textContent).toContain("Checking");
  expect(
    (screen.getByRole("button", { name: "Accept" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  expect(
    (screen.getByRole("button", { name: "Reject" }) as HTMLButtonElement)
      .disabled,
  ).toBe(false);
  expect(useCopilotProposalStore.getState().proposal?.acceptable).toBe(true);
  await act(async () =>
    resolve({
      data: { workflow_definition: workflow(step()).workflow_definition },
    }),
  );
  expect(
    (screen.getByRole("button", { name: "Accept" }) as HTMLButtonElement)
      .disabled,
  ).toBe(false);
});

it("shows a conversion error without blocking the chat settlement path", async () => {
  post.mockRejectedValue(new Error("conversion unavailable"));
  await renderProposal();
  expect(screen.getByRole("alert").textContent).toContain(
    "conversion unavailable",
  );
  expect(screen.getByRole("alert").textContent).toContain("chat card");
  expect(
    (screen.getByRole("button", { name: "Accept" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  expect(
    (screen.getByRole("button", { name: "Reject" }) as HTMLButtonElement)
      .disabled,
  ).toBe(false);
  expect(useCopilotProposalStore.getState().proposal?.acceptable).toBe(true);
  await useCopilotProposalStore.getState().proposal!.decide("approve");
  expect(decide).toHaveBeenCalledWith("approve");
});

it("reports an invalid conversion response instead of rendering a false diff", async () => {
  post.mockResolvedValue({ data: {} });
  await renderProposal();
  expect(screen.getByRole("alert").textContent).toContain("invalid definition");
  expect(
    (screen.getByRole("button", { name: "Accept" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
});

it("keeps the converted baseline when the chat republishes the same proposal with a new gate state", async () => {
  post.mockResolvedValue({
    data: { workflow_definition: workflow(step()).workflow_definition },
  });
  await renderProposal();
  expect(post).toHaveBeenCalledOnce();

  const current = useCopilotProposalStore.getState().proposal!;
  await act(async () => {
    useCopilotProposalStore
      .getState()
      .setProposal({ ...current, actionable: false, acceptable: false });
  });

  expect(post).toHaveBeenCalledOnce();
  expect(screen.queryByText(/Checking proposal changes/)).toBeNull();
});
