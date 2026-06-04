import { cleanup, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { RunEngine } from "@/api/types";

import { NavigationEditor } from "./NavigationEditor";
import { navigationNodeDefaultData, type NavigationNodeData } from "./types";

let nodeData: { type: "navigation"; data: NavigationNodeData } | null = null;

vi.mock("@xyflow/react", () => ({
  useNodesData: () => nodeData,
  useNodes: () => [],
  useEdges: () => [],
}));

vi.mock("@/components/EngineSelector", () => ({
  RunEngineSelector: ({
    availableEngines,
    value,
  }: {
    availableEngines: RunEngine[];
    value: RunEngine | null;
  }) => (
    <div
      data-testid="engine-selector"
      data-value={value ?? ""}
      data-available-engines={availableEngines.join(",")}
    />
  ),
}));

vi.mock("@/components/HelpTooltip", () => ({
  HelpTooltip: () => null,
}));

vi.mock("@/components/ModelSelector", () => ({
  ModelSelector: () => null,
}));

vi.mock("@/components/WorkflowBlockInput", () => ({
  WorkflowBlockInput: () => null,
}));

vi.mock("@/components/WorkflowBlockInputTextarea", () => ({
  WorkflowBlockInputTextarea: () => null,
}));

vi.mock("@/components/ui/accordion", () => ({
  Accordion: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  AccordionContent: ({ children }: { children?: ReactNode }) => (
    <div>{children}</div>
  ),
  AccordionItem: ({ children }: { children?: ReactNode }) => (
    <div>{children}</div>
  ),
  AccordionTrigger: ({ children }: { children?: ReactNode }) => (
    <div>{children}</div>
  ),
}));

vi.mock("@/components/ui/input", () => ({
  Input: () => null,
}));

vi.mock("@/components/ui/label", () => ({
  Label: ({ children }: { children?: ReactNode }) => <label>{children}</label>,
}));

vi.mock("@/components/ui/separator", () => ({
  Separator: () => null,
}));

vi.mock("@/components/ui/switch", () => ({
  Switch: () => null,
}));

vi.mock("../../useUpdate", () => ({
  useUpdate: () => vi.fn(),
}));

vi.mock("../../panels/useHasInteractedThisSession", () => ({
  useHasInteractedThisSession: () => true,
}));

vi.mock("../../workflowEditorUtils", () => ({
  getAvailableOutputParameterKeys: () => [],
  getParentLoopSkipsOnFail: () => false,
  isNodeInsideForLoop: () => false,
}));

vi.mock("../DisableCache", () => ({
  DisableCache: () => null,
}));

vi.mock("../IgnoreWorkflowSystemPrompt", () => ({
  IgnoreWorkflowSystemPrompt: () => null,
}));

vi.mock("../components/BlockExecutionOptions", () => ({
  BlockExecutionOptions: () => null,
}));

vi.mock("../TaskNode/ParametersMultiSelect", () => ({
  ParametersMultiSelect: () => null,
}));

vi.mock("@/routes/workflows/editor/ErrorCodeMappingEditor", () => ({
  ErrorCodeMappingEditor: () => null,
}));

vi.mock("../../hooks/useSelectedCredentialTotpIdentifier", () => ({
  useSelectedCredentialTotpIdentifier: () => null,
}));

function setNavigationNode(
  engine: RunEngine,
  overrides: Partial<NavigationNodeData> = {},
) {
  nodeData = {
    type: "navigation",
    data: {
      ...navigationNodeDefaultData,
      label: "navigate",
      engine,
      ...overrides,
    },
  };
}

afterEach(() => {
  cleanup();
  nodeData = null;
});

describe("NavigationEditor engine options", () => {
  test("does not offer V2 for new V1 navigation blocks", () => {
    setNavigationNode(RunEngine.SkyvernV1);

    render(<NavigationEditor blockId="nav-1" />);

    expect(
      screen
        .getByTestId("engine-selector")
        .getAttribute("data-available-engines"),
    ).toBe("skyvern-1.0,openai-cua,anthropic-cua");
  });

  test("keeps V2 available when editing an existing V2 block", () => {
    setNavigationNode(RunEngine.SkyvernV2);

    render(<NavigationEditor blockId="nav-1" />);

    expect(
      screen
        .getByTestId("engine-selector")
        .getAttribute("data-available-engines"),
    ).toBe("skyvern-1.0,skyvern-2.0,openai-cua,anthropic-cua");
  });

  test("keeps V2 available after a remount when a legacy V2 block has switched to V1", () => {
    setNavigationNode(RunEngine.SkyvernV1, { legacyV2Available: true });
    render(<NavigationEditor blockId="nav-1" />);

    expect(
      screen
        .getByTestId("engine-selector")
        .getAttribute("data-available-engines"),
    ).toBe("skyvern-1.0,skyvern-2.0,openai-cua,anthropic-cua");
  });
});
