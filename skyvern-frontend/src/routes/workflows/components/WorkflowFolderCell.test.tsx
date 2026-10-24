// @vitest-environment jsdom
import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  it,
  vi,
} from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { WorkflowFolderCell } from "./WorkflowFolderCell";
import type { Folder } from "../types/folderTypes";

const moveToFolder = vi.fn();
vi.mock("../hooks/useMoveWorkflowToFolder", () => ({
  useMoveWorkflowToFolder: () => ({ moveToFolder, isMoving: false }),
}));
vi.mock("../hooks/useFoldersQuery", () => ({
  useFoldersQuery: () => ({
    data: [
      { folder_id: "f1", title: "DC Apartment Hunt" },
      { folder_id: "f2", title: "Prospects" },
    ] as unknown as Folder[],
    isFetching: false,
  }),
}));

// cmdk and Radix Popper need ResizeObserver and scrollIntoView, which jsdom lacks.
class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
const originalScrollIntoView = Element.prototype.scrollIntoView;
beforeAll(() => {
  vi.stubGlobal("ResizeObserver", MockResizeObserver);
  Element.prototype.scrollIntoView = () => {};
});
afterAll(() => {
  vi.unstubAllGlobals();
  if (originalScrollIntoView) {
    Element.prototype.scrollIntoView = originalScrollIntoView;
  } else {
    delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
  }
});
afterEach(() => {
  moveToFolder.mockReset();
  cleanup();
});

function openPicker(name: RegExp) {
  const trigger = screen.getByRole("button", { name });
  fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false });
  fireEvent.click(trigger);
}

describe("WorkflowFolderCell", () => {
  it("shows the current folder name", () => {
    render(
      <WorkflowFolderCell
        workflowPermanentId="wpid_1"
        folderId="f1"
        folderTitle="DC Apartment Hunt"
        highlightQuery=""
      />,
    );

    expect(screen.getByText("DC Apartment Hunt")).toBeTruthy();
  });

  it("assigns the chosen folder from the dropdown", () => {
    render(
      <WorkflowFolderCell
        workflowPermanentId="wpid_1"
        folderId={null}
        folderTitle={null}
        highlightQuery=""
      />,
    );

    openPicker(/set folder/i);
    expect(screen.queryByText("Remove from folder")).toBeNull();
    fireEvent.click(screen.getByText("Prospects"));

    expect(moveToFolder).toHaveBeenCalledWith("f2");
  });

  it("removes the agent from its folder", () => {
    render(
      <WorkflowFolderCell
        workflowPermanentId="wpid_1"
        folderId="f1"
        folderTitle="DC Apartment Hunt"
        highlightQuery=""
      />,
    );

    openPicker(/change folder/i);
    fireEvent.click(screen.getByText("Remove from folder"));

    expect(moveToFolder).toHaveBeenCalledWith(null);
  });

  it("keeps picker clicks from bubbling to row handlers", () => {
    const onRowClick = vi.fn();
    render(
      <div onClick={onRowClick}>
        <WorkflowFolderCell
          workflowPermanentId="wpid_1"
          folderId={null}
          folderTitle={null}
          highlightQuery=""
        />
      </div>,
    );

    openPicker(/set folder/i);
    onRowClick.mockReset();
    fireEvent.click(screen.getByText("Prospects"));

    expect(onRowClick).not.toHaveBeenCalled();
  });
});
