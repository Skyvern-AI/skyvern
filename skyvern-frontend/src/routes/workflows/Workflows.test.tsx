// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Workflows } from "./Workflows";

const useWorkflowsDirectoryTreeMock = vi.fn();

vi.mock("@/hooks/useWorkflowsDirectoryTree", () => ({
  useWorkflowsDirectoryTree: () => useWorkflowsDirectoryTreeMock(),
}));

vi.mock("./WorkflowsTree", () => ({
  WorkflowsTree: () => <div data-testid="workflows-tree" />,
}));

vi.mock("./WorkflowsFlat", () => ({
  WorkflowsFlat: () => <div data-testid="workflows-flat" />,
}));

describe("Workflows", () => {
  afterEach(() => {
    cleanup();
    useWorkflowsDirectoryTreeMock.mockReset();
  });

  it("renders the flat folders/list when the directory-tree preview is off", () => {
    useWorkflowsDirectoryTreeMock.mockReturnValue(false);

    render(<Workflows />);

    expect(screen.getByTestId("workflows-flat")).toBeTruthy();
    expect(screen.queryByTestId("workflows-tree")).toBeNull();
  });

  it("renders the directory tree when the preview is opted in", () => {
    useWorkflowsDirectoryTreeMock.mockReturnValue(true);

    render(<Workflows />);

    expect(screen.getByTestId("workflows-tree")).toBeTruthy();
    expect(screen.queryByTestId("workflows-flat")).toBeNull();
  });
});
