// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { RunBlockingSurface } from "./RunBlockingSurface";
import { useLocateBlockStore } from "./useLocateBlockStore";
import { useRunValidationStore } from "./useRunValidationStore";

function renderAt(search: string) {
  return render(
    <MemoryRouter initialEntries={[`/workflows/x/build${search}`]}>
      <RunBlockingSurface />
    </MemoryRouter>,
  );
}

describe("RunBlockingSurface", () => {
  beforeEach(() => {
    useRunValidationStore.getState().setBlockingBlocks([]);
    useLocateBlockStore.getState().clearLocate();
  });
  afterEach(() => {
    cleanup();
    useRunValidationStore.getState().setBlockingBlocks([]);
    useLocateBlockStore.getState().clearLocate();
  });

  test("renders nothing when no blocks are run-blocking", () => {
    const { container } = renderAt("");
    expect(container.firstChild).toBeNull();
  });

  test("renders nothing when disabled", () => {
    useRunValidationStore
      .getState()
      .setBlockingBlocks([{ id: "n2", label: "block_2" }]);
    const { container } = render(
      <MemoryRouter>
        <RunBlockingSurface enabled={false} />
      </MemoryRouter>,
    );
    expect(container.firstChild).toBeNull();
  });

  test("lists every blocking block", () => {
    useRunValidationStore.getState().setBlockingBlocks([
      { id: "n2", label: "block_2" },
      { id: "n3", label: "block_3" },
    ]);
    renderAt("");
    expect(screen.getByText("2 blocks need fixing")).toBeTruthy();
    expect(screen.getByText("block_2")).toBeTruthy();
    expect(screen.getByText("block_3")).toBeTruthy();
  });

  test("clicking a block requests locate with its node id", () => {
    useRunValidationStore
      .getState()
      .setBlockingBlocks([{ id: "n2", label: "block_2" }]);
    renderAt("");
    fireEvent.click(screen.getByText("block_2"));
    expect(useLocateBlockStore.getState().request?.nodeId).toBe("n2");
  });

  test("keeps obsolete variant query params on the final panel", () => {
    useRunValidationStore
      .getState()
      .setBlockingBlocks([{ id: "n2", label: "block_2" }]);
    renderAt("?rbv=b");
    expect(screen.getByText("1 block needs fixing")).toBeTruthy();
    expect(screen.getByText("block_2")).toBeTruthy();
  });
});
