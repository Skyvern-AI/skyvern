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

import { FolderPickerCommand } from "./FolderPickerCommand";
import type { Folder } from "../types/folderTypes";

const foldersResult: { data: Folder[]; isFetching: boolean } = {
  data: [],
  isFetching: false,
};
vi.mock("../hooks/useFoldersQuery", () => ({
  useFoldersQuery: () => foldersResult,
}));

// cmdk needs ResizeObserver and scrollIntoView, which jsdom lacks.
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
  foldersResult.data = [];
  foldersResult.isFetching = false;
  cleanup();
});

function folder(folder_id: string, title: string): Folder {
  return { folder_id, title } as unknown as Folder;
}

describe("FolderPickerCommand", () => {
  it("shows the empty state when no folders match", () => {
    render(<FolderPickerCommand currentFolderId={null} onSelect={() => {}} />);

    expect(screen.getByText("No folders found.")).toBeTruthy();
  });

  it("reports the chosen folder id", () => {
    foldersResult.data = [folder("f1", "Prospects")];
    const onSelect = vi.fn();
    render(<FolderPickerCommand currentFolderId={null} onSelect={onSelect} />);

    fireEvent.click(screen.getByText("Prospects"));

    expect(onSelect).toHaveBeenCalledWith("f1");
  });

  it("offers Remove from folder and reports a null selection", () => {
    const onSelect = vi.fn();
    render(
      <FolderPickerCommand
        currentFolderId="f1"
        showRemove
        onSelect={onSelect}
      />,
    );

    fireEvent.click(screen.getByText("Remove from folder"));

    expect(onSelect).toHaveBeenCalledWith(null);
  });
});
